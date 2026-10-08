"""`bench` command line: run | report | validate | list | fixtures."""

import argparse
import json
import logging
import os
import sys
import tempfile
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from bench import CATEGORIES, logs
from bench.config import Settings, load_dotenv
from bench.fixtures_gen import generate
from bench.report import by_category_model, by_task_type_model, parse_since, suggest, table
from bench.routing_patch import PatchError, apply_changes, plan_changes, unified_diff
from bench.tasks import TaskError, lint, load_agents, load_routing, load_tasks, select

EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2
log = logging.getLogger("bench")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bench", description="AIOS V1 test battery (Hermes + Decision Service)")
    p.add_argument("-v", "--verbose", action="count", default=0)
    p.add_argument("--log-format", choices=("json", "text"), default=os.environ.get("BENCH_LOG_FORMAT", "json"))
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="submit tasks to Hermes, check them, store results in bench_runs")
    r.add_argument("--category", action="append", choices=list(CATEGORIES), help="repeatable")
    r.add_argument("--task", action="append", metavar="KEY", help="repeatable")
    r.add_argument("--repeat", type=int, default=1, help="runs per task (default 1)")
    r.add_argument("--model", metavar="ALIAS",
                   help="pin this routing.yaml model as the run's start model (Decision /v1/route `model`; "
                        "escalation still follows the ladder, capped by the agent's max_tier)")
    r.add_argument("--dry-run", action="store_true", help="print the plan; call nothing")
    r.add_argument("--local", action="store_true", help="stage fixtures and run command checks on this machine")
    r.add_argument("--keep-workdir", action="store_true", help="leave staged workdirs for inspection")
    r.add_argument("--no-db", action="store_true", help="print rows as JSON lines instead of writing bench_runs")

    rep = sub.add_parser("report", help="success and cost per success by category x model + routing.yaml diff")
    rep.add_argument("--since", help="7d | 24h | 2w | 2026-10-01 | ISO timestamp")
    rep.add_argument("--min-runs", type=int, default=3, help="runs a (task type, model) needs to be suggested")
    rep.add_argument("--target", type=float, help="success rate floor (default routing.yaml learning.target_success_rate)")
    rep.add_argument("--json", action="store_true")

    sub.add_parser("validate", help="lint every task (JSON schema, fixtures, routing/agents refs, category counts)")
    ls = sub.add_parser("list", help="list tasks")
    ls.add_argument("--category", action="append", choices=list(CATEGORIES))
    fx = sub.add_parser("fixtures", help="generate media fixtures (tone always; speech/video need espeak-ng/ffmpeg)")
    fx.add_argument("--force", action="store_true", help="regenerate existing files")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logs.setup(args.log_format, args.verbose)
    load_dotenv()
    settings = Settings.from_env(os.environ)
    handlers = {"run": cmd_run, "report": cmd_report, "validate": cmd_validate, "list": cmd_list,
                "fixtures": cmd_fixtures}
    try:
        return handlers[args.command](args, settings)
    except TaskError as exc:
        print("\n".join(exc.problems), file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        return 130


# ---------------------------------------------------------------- validate / list / fixtures

def cmd_validate(args, s: Settings) -> int:
    routing = load_routing(s.routing_file) if s.routing_file.is_file() else None
    agents = load_agents(s.agents_dir) if s.agents_dir.is_dir() else None
    tasks, problems = lint(s.tasks_dir, s.fixtures_dir, routing, agents)
    counts = {c: sum(t.category == c for t in tasks) for c in CATEGORIES}
    print(f"{len(tasks)} tasks: " + ", ".join(f"{c} {n}/{CATEGORIES[c]}" for c, n in counts.items()))
    if routing is None or agents is None:
        print("warning: routing.yaml or agents/ not found; task_type/agent references not checked", file=sys.stderr)
    if problems:
        print("\n".join(f"  ✘ {p}" for p in problems))
        print(f"{len(problems)} problem(s)")
        return EXIT_FAIL
    print("ok")
    return EXIT_OK


def cmd_list(args, s: Settings) -> int:
    for t in select(load_tasks(s.tasks_dir), args.category):
        print(f"{t.category:9}  {t.key:34}  {t.check['type']:11}  {t.agent or '-':11}  {t.tenant:7}  {t.title}")
    return EXIT_OK


def cmd_fixtures(args, s: Settings) -> int:
    results = generate(s.fixtures_dir, force=args.force)
    for r in results:
        print(f"{r.status:8} {r.fixture}/{r.name}" + (f"  ({r.detail})" if r.detail else ""))
    if any(r.status == "skipped" for r in results):
        print("speech/video fixtures are optional: install espeak-ng (+ ffmpeg) to enable those media tasks")
    return EXIT_FAIL if any(r.status == "failed" for r in results) else EXIT_OK


# ---------------------------------------------------------------- run

def cmd_run(args, s: Settings) -> int:
    from bench.runner import RunOptions, plan

    if args.repeat < 1:
        print("--repeat must be >= 1", file=sys.stderr)
        return EXIT_USAGE
    tasks = select(load_tasks(s.tasks_dir), args.category, args.task)
    if not tasks:
        print("no tasks selected", file=sys.stderr)
        return EXIT_USAGE
    routing = load_routing(s.routing_file)
    models = set((routing.get("models") or {}).keys())
    if args.model and models and args.model not in models:
        print(f"--model {args.model} is not a model alias in {s.routing_file}: {', '.join(sorted(models))}",
              file=sys.stderr)
        return EXIT_USAGE
    options = RunOptions(repeat=args.repeat, model=args.model, keep_workdir=args.keep_workdir)
    if args.dry_run:
        for item in plan(tasks, options, s.fixtures_dir, s.profile_keys, datetime.now(UTC)):
            print(json.dumps(item, ensure_ascii=False))
        return EXIT_OK
    return _execute(args, s, tasks, options)


def _execute(args, s: Settings, tasks, options) -> int:
    from bench.decision import DecisionClient
    from bench.hermes import HermesClient
    from bench.judge import LiteLLMJudge
    from bench.runner import Runner
    from bench.store import MemoryStore, PostgresStore, StoreError
    from bench.workspace import LocalWorkspace, SSHWorkspace, WorkspaceError

    if not s.hermes_api_key:
        print("HERMES_API_KEY is not set", file=sys.stderr)
        return EXIT_USAGE
    try:
        if args.local:
            workspace = LocalWorkspace(Path(s.workspace or Path(tempfile.gettempdir()) / "aios-bench"))
        elif s.ssh is not None:
            workspace = SSHWorkspace(s.ssh, s.workspace or "/workspace/bench")
        else:
            workspace = None
    except WorkspaceError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE
    if workspace is None and any(t.needs_workdir for t in tasks):
        print("tasks with fixtures need TERMINAL_SSH_HOST (sandbox) or --local", file=sys.stderr)
        return EXIT_USAGE
    _warn_profiles(tasks, s, args.repeat)
    judge = None
    if any(t.check["type"] == "llm_judge" for t in tasks):
        if s.litellm_key:
            judge = LiteLLMJudge(s.litellm_url, s.litellm_key, s.judge_model)
        else:
            log.warning("BENCH_LITELLM_KEY not set: llm_judge tasks will be recorded as errors")
    if args.no_db:
        store = MemoryStore()
    else:
        if not s.database_url:
            print("set BENCH_DATABASE_URL (or AIOS_DB_PASSWORD) or pass --no-db", file=sys.stderr)
            return EXIT_USAGE
        try:
            store = PostgresStore(s.database_url)
        except StoreError as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_FAIL

    runner = Runner(fixtures_dir=s.fixtures_dir,
                    hermes=HermesClient(s.hermes_url, s.hermes_api_key, s.profile_keys, s.http_timeout),
                    decision=DecisionClient(s.decision_url, s.decision_api_key, s.http_timeout),
                    workspace=workspace, judge=judge, store=store, task_timeout=s.task_timeout,
                    poll_interval=s.poll_interval, prices=load_routing(s.routing_file).get("models") or {})
    try:
        summary = runner.run(tasks, options)
    finally:
        if isinstance(store, PostgresStore):
            store.close()
    if args.no_db:
        for row in summary.rows:
            print(json.dumps(row.as_dict(), ensure_ascii=False, default=str))
    else:
        print(render_run_summary(summary))
    return EXIT_FAIL if summary.errors or summary.store_errors else EXIT_OK


def _warn_profiles(tasks, s: Settings, repeat: int) -> None:
    named = sorted({t.agent for t in tasks if t.agent not in (None, "chief")})
    unrouted = [p for p in named if p not in s.profile_keys]
    if unrouted:
        log.warning("no Hermes API key for profile(s) %s: their tasks run on the default profile (chief), which has "
                    "no shell; set HERMES_API_KEY_<PROFILE> (see bench/README.md)", ", ".join(unrouted),
                    extra={"profiles": unrouted, "tasks": sum(1 for t in tasks if t.agent in unrouted) * repeat})

def render_run_summary(summary) -> str:
    lines = [f"batch {summary.batch_id}"]
    for r in summary.rows:
        verdict = "ERROR" if r.error else "PASS " if r.success else "FAIL "
        model = f"{r.start_model or '?'}->{r.final_model or '?'}"
        lines.append(f"{verdict} {r.task_key:34} #{r.repeat_index}  {model:28} ${r.cost_usd:.4f}  "
                     f"{r.duration_ms / 1000:6.1f}s" + (f"  {r.error[:80]}" if r.error else ""))
    for key, reason in summary.skipped:
        lines.append(f"SKIP  {key:34} {reason}")
    passed = sum(r.success for r in summary.rows)
    lines.append(f"{passed}/{len(summary.rows)} passed, {summary.errors} error(s), {len(summary.skipped)} skipped")
    return "\n".join(lines)


# ---------------------------------------------------------------- report

def cmd_report(args, s: Settings) -> int:
    from bench.store import PostgresStore, StoreError

    try:
        since = parse_since(args.since)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE
    if not s.database_url:
        print("set BENCH_DATABASE_URL (or AIOS_DB_PASSWORD)", file=sys.stderr)
        return EXIT_USAGE
    try:
        store = PostgresStore(s.database_url)
        rows = store.fetch(since)
        store.close()
    except StoreError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_FAIL
    routing_text = s.routing_file.read_text(encoding="utf-8") if s.routing_file.is_file() else ""
    text, data = build_report(rows, routing_text, since=since, target=args.target, min_runs=args.min_runs)
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str) if args.json else text)
    return EXIT_OK


def build_report(rows: list[dict], routing_text: str, *, since: datetime | None = None,
                 target: float | None = None, min_runs: int = 3) -> tuple[str, dict]:
    import yaml

    routing = (yaml.safe_load(routing_text) if routing_text else None) or {}
    if target is None:
        target = float((routing.get("learning") or {}).get("target_success_rate") or 0.8)
    models = set((routing.get("models") or {}).keys()) or None
    cat = by_category_model(rows)
    tt = by_task_type_model(rows)
    suggestions = suggest(rows, target_success=target, min_runs=min_runs, known_models=models)
    changes = plan_changes(suggestions, routing)
    diff, patch_error = "", None
    if changes:
        try:
            diff = unified_diff(routing_text, apply_changes(routing_text, changes))
        except PatchError as exc:
            patch_error = str(exc)

    errors = sum(1 for r in rows if r.get("error"))
    head = f"bench report: {len(rows)} run(s)" + (f" since {since.isoformat()}" if since else "") + \
        (f", {errors} infrastructure error(s) excluded from rates" if errors else "")
    out = [head, "", "## category x start model (§14 cost per successful task)", table(cat, ("category", "start_model")),
           "", "## task type x start model", table(tt, ("task_type", "start_model")), "",
           f"## suggested routing (lowest cost per success with success >= {target:.0%}, >= {min_runs} runs)"]
    if suggestions:
        for sg in suggestions:
            levels = ", ".join(sg.complexities) or "no complexity recorded"
            out.append(f"#   {sg.task_type}: {sg.model} (success {sg.success_rate:.0%}, "
                       f"${sg.cost_per_success:.4f}/success, {sg.runs} runs; benchmarked at: {levels})")
    else:
        out.append("#   no (task type, model) pair qualifies yet")
    if patch_error:
        out.append(f"# could not build the patch: {patch_error}")
    elif diff:
        out += ["# suggested config/routing.yaml patch (NOT applied; review, then apply by hand):", diff.rstrip("\n")]
    elif suggestions:
        out.append("# routing.yaml already matches the suggestions: no patch")

    data = {
        "since": since, "runs": len(rows), "errors": errors, "target_success_rate": target, "min_runs": min_runs,
        "by_category_model": [{"category": k[0], "start_model": k[1], **v.as_dict()} for k, v in cat.items()],
        "by_task_type_model": [{"task_type": k[0], "start_model": k[1], **v.as_dict()} for k, v in tt.items()],
        "suggestions": [asdict(sg) for sg in suggestions],
        "changes": [asdict(c) for c in changes], "routing_patch": diff, "patch_error": patch_error,
    }
    return "\n".join(out), data
