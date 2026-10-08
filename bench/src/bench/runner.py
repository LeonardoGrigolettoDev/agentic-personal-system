"""Run tasks end to end: stage fixture -> pre-route -> Hermes run -> check -> ledger -> finish -> bench_runs."""

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from bench.checks import CheckResult, Judge, evaluate, sha256_files
from bench.decision import DecisionClient, DecisionError
from bench.fixtures_gen import missing_files
from bench.hermes import HermesClient, HermesError, RunOutcome, resolve_profile
from bench.preflight import Preflight
from bench.store import BenchRow, Store, StoreError
from bench.tasks import Task
from bench.workspace import Workspace, WorkspaceError

log = logging.getLogger("bench.runner")

SETUP_TIMEOUT = 300
ANSWER_EXCERPT = 2000
# the aios plugin posts /v1/usage from a background pool after each LLM call: re-read the ledger until it settles
SETTLE_READS = 5
SETTLE_INTERVAL = 1.0


class SetupError(Exception):
    pass


def estimate_cost(prices: dict[str, dict], model: str | None, input_tokens: int, output_tokens: int,
                  cache_read_tokens: int) -> float | None:
    """Same formula as the Decision Service (decision/internal/policy Policy.Cost), USD per 1M tokens from
    routing.yaml `models.<alias>.price`. None when the model is not a known alias."""
    price = ((prices or {}).get(model or "") or {}).get("price")
    if not price:
        return None
    cached = price.get("cache_read") or price.get("input", 0)
    fresh = max(input_tokens - cache_read_tokens, 0)
    return (fresh * price.get("input", 0) + cache_read_tokens * cached + output_tokens * price.get("output", 0)) / 1e6


@dataclass(frozen=True)
class RunOptions:
    repeat: int = 1
    model: str | None = None
    keep_workdir: bool = False


@dataclass
class RunSummary:
    batch_id: str
    rows: list[BenchRow] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (task key, reason)
    store_errors: int = 0

    @property
    def errors(self) -> int:
        return sum(1 for r in self.rows if r.error)


def session_id(task_key: str, n: int, ts: str) -> str:
    return f"bench-{task_key}-{n}-{ts}"


def batch_stamp(now: datetime) -> str:
    return now.astimezone(UTC).strftime("%Y%m%d%H%M%S")


def plan(tasks: list[Task], options: RunOptions, fixtures_dir: Path, profile_keys: dict[str, str],
         now: datetime) -> list[dict]:
    """What `bench run` would do, without touching any service (--dry-run)."""
    ts = batch_stamp(now)
    out = []
    for task in tasks:
        _, served = resolve_profile(task.agent, profile_keys)
        missing = missing_files(fixtures_dir, task.fixture, task.generated_files)
        for n in range(1, options.repeat + 1):
            out.append({
                "session_id": session_id(task.key, n, ts), "task": task.key, "category": task.category,
                "tenant": task.tenant, "agent": task.agent, "hermes_profile": served, "model": options.model,
                "check": task.check["type"], "fixture": task.fixture, "setup": task.setup,
                "requires": list(task.requires), "timeout_s": task.timeout_s,
                "skip": f"missing generated files: {', '.join(missing)}" if missing else None,
            })
    return out


class Runner:
    def __init__(self, *, fixtures_dir: Path, hermes: HermesClient, decision: DecisionClient | None,
                 workspace: Workspace | None, judge: Judge | None, store: Store, task_timeout: float = 900,
                 poll_interval: float = 2.0, prices: dict[str, dict] | None = None,
                 preflight: Preflight | None = None, settle_interval: float = SETTLE_INTERVAL,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self.fixtures_dir = fixtures_dir
        self.hermes = hermes
        self.decision = decision if decision is not None and decision.enabled else None
        self.workspace = workspace
        self.judge = judge
        self.store = store
        self.task_timeout = task_timeout
        self.poll_interval = poll_interval
        self.prices = prices or {}
        self.preflight = preflight or Preflight(workspace)
        self.settle_interval = settle_interval
        self.sleep = sleep
        self.clock = clock

    def run(self, tasks: list[Task], options: RunOptions) -> RunSummary:
        started = self.clock()
        summary = RunSummary(batch_id=f"{batch_stamp(started)}-{uuid.uuid4().hex[:6]}")
        if self.decision is None:
            log.warning("Decision Service not configured: no cost/tier data, outcomes not reported to routing")
        ts = batch_stamp(started)
        for task in tasks:
            reason = self._skip_reason(task)
            if reason:
                log.warning("skipping task", extra={"task": task.key, "reason": reason})
                summary.skipped.append((task.key, reason))
                continue
            for n in range(1, options.repeat + 1):
                row = self.run_one(task, n, ts, summary.batch_id, options)
                summary.rows.append(row)
                try:
                    self.store.insert(row)
                except StoreError as exc:
                    summary.store_errors += 1
                    log.error("result not stored: %s", exc, extra={"session_id": row.session_id})
        return summary

    def _skip_reason(self, task: Task) -> str | None:
        """Tasks whose inputs or services are missing are skipped: running them would only record a fake failure."""
        missing = missing_files(self.fixtures_dir, task.fixture, task.generated_files)
        if missing:
            return f"missing generated files {', '.join(missing)} (run `make bench-fixtures`)"
        return self.preflight.unmet(task.requires)

    def run_one(self, task: Task, n: int, ts: str, batch_id: str, options: RunOptions) -> BenchRow:
        sid = session_id(task.key, n, ts)
        _, served = resolve_profile(task.agent, self.hermes.profile_keys)
        route_agent = "chief" if served == "default" else served
        row = BenchRow(run_at=self.clock(), batch_id=batch_id, task_key=task.key, category=task.category,
                       repeat_index=n, session_id=sid, tenant=task.tenant, success=False,
                       check_type=task.check["type"], duration_ms=0, agent=task.agent, hermes_profile=served,
                       requested_model=options.model, expected_task_type=task.expected_task_type,
                       expected_domain=task.expected_domain)
        log.info("task start", extra={"task": task.key, "session_id": sid, "profile": served})
        workdir: str | None = None
        outcome: RunOutcome | None = None
        result: CheckResult | None = None
        try:
            workdir = self._stage(task, sid)
            self._setup(task, workdir)
            protect = task.check.get("protect") or []
            hashes = sha256_files(self.fixtures_dir / task.fixture, protect) if protect and task.fixture else None
            prompt = task.render_prompt(workdir)
            self._preroute(sid, prompt, task, route_agent, options.model)
            t0 = time.monotonic()
            submitted = self.hermes.submit(prompt=prompt, session_id=sid, profile=task.agent, model=options.model)
            row.hermes_run_id = submitted.run_id
            outcome = self.hermes.wait(submitted, timeout=float(task.timeout_s or self.task_timeout),
                                       poll_interval=self.poll_interval)
            row.duration_ms = int((time.monotonic() - t0) * 1000)
            row.hermes_status, row.tool_calls = outcome.status, outcome.tool_calls
            if outcome.infrastructure_error:
                row.error = outcome.infrastructure_error
                log.error("hermes run ended without a verdict", extra={"task": task.key, "session_id": sid,
                                                                       "error": row.error})
            elif outcome.status == "timeout":
                # never judged: the tree may be half written, and the agent ran out of time anyway
                result = CheckResult(False, {"type": task.check["type"], "evaluated": False, "reason": "timeout"})
            else:
                result = evaluate(task.check, answer=outcome.output, prompt=prompt, workspace=self.workspace,
                                  workdir=workdir, judge=self.judge, protect_hashes=hashes)
                row.success = result.passed and result.error is None
                row.error = result.error
        except (WorkspaceError, SetupError, HermesError, OSError) as exc:
            row.error = f"{type(exc).__name__}: {exc}"
            log.error("task error", extra={"task": task.key, "session_id": sid, "error": row.error})
        row.check_detail = self._detail(result, outcome)
        self._ledger(row, task, outcome, route_agent)
        self._cleanup(workdir, options, outcome, row)
        log.info("task done", extra={"task": task.key, "session_id": sid, "success": row.success,
                                     "model": row.start_model, "cost_usd": row.cost_usd, "error": row.error})
        return row

    def _stage(self, task: Task, sid: str) -> str | None:
        if not task.needs_workdir:
            return None
        if self.workspace is None:
            raise SetupError("task needs a workspace (sandbox SSH or --local) but none is configured")
        fixture = self.fixtures_dir / task.fixture if task.fixture else None
        return self.workspace.stage(fixture, sid)

    def _setup(self, task: Task, workdir: str | None) -> None:
        if not task.setup or workdir is None:
            return
        res = self.workspace.run(workdir, task.setup, SETUP_TIMEOUT)
        if res.exit_code != 0:
            raise SetupError(f"setup exited {res.exit_code}: {res.output_tail[-500:]}")

    def _preroute(self, sid: str, prompt: str, task: Task, agent: str, model: str | None = None) -> None:
        if self.decision is None:
            return
        try:
            self.decision.route(session_id=sid, text=prompt, tenant=task.tenant, agent=agent, model=model)
        except DecisionError as exc:
            if model and exc.status is not None and 400 <= exc.status < 500:
                # refused pin (unknown model, above max_tier): running unpinned would not measure `model`. When
                # the service is down, the plugin fails open and Hermes honours the run's own `model` instead.
                raise SetupError(f"pre-route pinning {model} failed: {exc}") from exc
            log.warning("pre-route failed (the aios plugin will route): %s", exc, extra={"session_id": sid})

    @staticmethod
    def _detail(result: CheckResult | None, outcome: RunOutcome | None) -> dict:
        detail = dict(result.detail) if result else {"evaluated": False}
        if outcome is not None:
            detail["hermes"] = {"status": outcome.status, "error": outcome.error, "usage": outcome.usage,
                                "runtime": outcome.runtime, "approvals_denied": outcome.approvals_denied}
            detail["answer_excerpt"] = outcome.output[:ANSWER_EXCERPT].replace("\x00", "")
        return detail

    def _ledger(self, row: BenchRow, task: Task, outcome: RunOutcome | None, route_agent: str) -> None:
        """Copy the ledger's view of the run into the row, then report the check's verdict to routing."""
        run = self._settled_run(row, outcome) if self.decision is not None else None
        if run:
            self._copy_run(row, task, run, route_agent)
        if row.input_tokens + row.output_tokens == 0 and outcome is not None:
            self._usage_from_hermes(row, outcome)
        if not run:
            row.check_detail["ledger"] = "unavailable"
            row.start_model = row.requested_model or row.final_model
            return
        if row.requested_model and row.start_model != row.requested_model:
            row.check_detail["model_override"] = "ignored"  # the run existed before the pin (or no pre-route)
        status = "cancelled" if row.error else "succeeded" if row.success else "failed"
        try:
            self.decision.finish(row.session_id, status, task.expected_task_type)
            row.check_detail["ledger_finish"] = status
        except DecisionError as exc:
            log.warning("finish not recorded: %s", exc, extra={"session_id": row.session_id})

    def _settled_run(self, row: BenchRow, outcome: RunOutcome | None) -> dict | None:
        """The ledger run once the plugin's last /v1/usage has landed: its input tokens reach Hermes' own count, or
        llm_calls is unchanged between two reads (at most SETTLE_READS reads). Without a Hermes outcome nothing ran
        to completion, so one read is enough."""
        target = int(((outcome.usage if outcome else None) or {}).get("input_tokens") or 0)
        run, reads = None, 0
        try:
            run, reads = self.decision.get_run(row.session_id), 1
            while run and outcome is not None and reads < SETTLE_READS:
                if target and int(run.get("input_tokens") or 0) >= target:
                    break
                self.sleep(self.settle_interval)
                again, reads = self.decision.get_run(row.session_id), reads + 1
                if not again:
                    break
                stable = again.get("llm_calls") == run.get("llm_calls")
                run = again
                if stable:
                    break
        except DecisionError as exc:
            log.warning("ledger read failed: %s", exc, extra={"session_id": row.session_id})
        row.check_detail["ledger_settle_reads"] = reads
        return run

    @staticmethod
    def _copy_run(row: BenchRow, task: Task, run: dict, route_agent: str) -> None:
        row.start_model = run.get("start_model") or run.get("model")
        row.final_model = run.get("model")
        row.tier = run.get("tier")
        row.task_type = run.get("task_type") or None
        row.domain = run.get("domain") or None
        row.complexity = run.get("complexity") or None
        row.input_tokens = int(run.get("input_tokens") or 0)
        row.output_tokens = int(run.get("output_tokens") or 0)
        row.cache_read_tokens = int(run.get("cache_read_tokens") or 0)
        row.cost_usd = float(run.get("cost_usd") or 0)
        row.iterations = int(run.get("iterations") or 0)
        row.repairs = int(run.get("repairs") or 0)
        row.escalations = int(run.get("escalations") or 0)
        if task.expected_task_type and row.task_type:
            row.router_task_type_ok = row.task_type == task.expected_task_type
        # a named agent makes the Decision Service copy that agent's domain without asking the classifier
        if (run.get("agent") or route_agent) != "chief":
            row.check_detail["domain_pinned_by_agent"] = True
        elif task.expected_domain and row.domain:
            row.router_domain_ok = row.domain == task.expected_domain

    def _usage_from_hermes(self, row: BenchRow, outcome: RunOutcome) -> None:
        """No usage in the ledger (Decision Service down, or the aios plugin not reporting): take Hermes' own
        counters and price them like the Decision Service would, so cost per success stays comparable."""
        usage, runtime = outcome.usage or {}, outcome.runtime or {}
        row.input_tokens = int(usage.get("input_tokens") or 0)
        row.output_tokens = int(usage.get("output_tokens") or 0)
        row.cache_read_tokens = int(usage.get("cache_read_tokens") or 0)
        served = runtime.get("model") or None
        row.final_model = row.final_model or served
        if row.input_tokens + row.output_tokens == 0:
            return
        row.check_detail["usage_source"] = "hermes"
        cost = estimate_cost(self.prices, served, row.input_tokens, row.output_tokens, row.cache_read_tokens)
        if cost is not None:
            row.cost_usd = cost

    def _cleanup(self, workdir: str | None, options: RunOptions, outcome: RunOutcome | None, row: BenchRow) -> None:
        if not workdir or self.workspace is None or options.keep_workdir:
            return
        if outcome is not None and not outcome.settled:
            row.check_detail["workdir_kept"] = workdir
            log.warning("workdir kept: the stopped run is still not terminal", extra={"workdir": workdir})
            return
        try:
            self.workspace.remove(workdir)
        except WorkspaceError as exc:
            log.warning("workdir not removed: %s", exc, extra={"workdir": workdir})
