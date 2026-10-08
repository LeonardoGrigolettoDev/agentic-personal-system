"""Hermes API server client for the Runs API (verified against hermes-agent v2026.9.24,
gateway/platforms/api_server_runs.py):

- POST /v1/runs {input, session_id, instructions, model?} + Idempotency-Key -> 202 {run_id, status: "started"}
  (429 "Too many concurrent runs" -> back off and retry; the idempotency key makes retries safe)
- GET /v1/runs/{run_id}/events -> SSE `data: {"event": ...}` frames (tool.started, approval.request,
  run.completed|failed|cancelled|interrupted), `: keepalive` comments every 10 s, `: stream closed` at the end
- GET /v1/runs/{run_id} -> {status, output, usage, runtime, error, ...}; terminal statuses are kept for 1 h
- POST /v1/runs/{run_id}/approval {"choice": "deny", "all": true}; POST /v1/runs/{run_id}/stop
- named profiles are served under /p/<profile>/ and need that profile's own API_SERVER_KEY
"""

import json
import logging
import time
from dataclasses import dataclass, field

import httpx

log = logging.getLogger("bench.hermes")

TERMINAL = {"completed", "failed", "cancelled", "interrupted"}
TERMINAL_EVENTS = {f"run.{s}" for s in TERMINAL}
INSTRUCTIONS = (
    "Você está executando uma tarefa do benchmark automático do AI Agent OS. Trabalhe de forma autônoma: não peça "
    "confirmação nem esclarecimentos; se faltar informação, assuma o razoável e diga o que assumiu. Responda em "
    "português do Brasil, salvo quando a tarefa pedir outro idioma. Quando a tarefa pedir um formato (JSON, só o "
    "comando, lista numerada...), a resposta final deve seguir exatamente esse formato. "
    "Isto é um teste automatizado e os dados pessoais do enunciado são fictícios: NÃO salve memórias (memory, "
    "memory_save, ingest_note), NÃO crie tarefas Kanban nem cron, NÃO envie mensagens; resolva e responda nesta "
    "mesma sessão, sem delegação assíncrona."
)
STOP_GRACE = 60.0  # seconds to wait for a stopped run to reach a terminal status


class HermesError(Exception):
    pass


@dataclass(frozen=True)
class SubmittedRun:
    run_id: str
    prefix: str
    api_key: str
    profile: str


@dataclass
class RunOutcome:
    status: str  # completed | failed | cancelled | interrupted | timeout
    output: str = ""
    usage: dict = field(default_factory=dict)
    runtime: dict = field(default_factory=dict)
    error: str | None = None
    tool_calls: int | None = None
    approvals_denied: int = 0
    settled: bool = True  # False: still not terminal after a timeout's /stop (the agent may still be working)

    @property
    def infrastructure_error(self) -> str | None:
        """Terminal statuses that are no verdict on the agent: the gateway went down (`interrupted`), something
        other than the bench stopped the run (`cancelled`; the bench's own stop is reported as `timeout`), or the
        turn died on a provider/runtime error (`failed` with `error`: LiteLLM down, 400 budget exceeded, 401...).
        `failed` without `error` is the agent's own outcome (iteration budget, partial turn)."""
        if self.status in ("interrupted", "cancelled") or (self.status == "failed" and self.error):
            return f"hermes {self.status}: {self.error or 'run ended without an answer'}"[:500]
        return None


def resolve_profile(profile: str | None, profile_keys: dict[str, str]) -> tuple[str, str]:
    """(URL prefix, served profile). Named profiles need their own key; otherwise the default profile serves."""
    if profile and profile not in ("chief", "default") and profile_keys.get(profile):
        return f"/p/{profile}", profile
    return "", "default"


class HermesClient:
    def __init__(self, base_url: str, api_key: str, profile_keys: dict[str, str] | None = None,
                 timeout: float = 30, client: httpx.Client | None = None, sleep=time.sleep,
                 stop_grace: float = STOP_GRACE):
        if not api_key:
            raise ValueError("HERMES_API_KEY is required")
        self.base = base_url.rstrip("/")
        self.api_key = api_key
        self.profile_keys = profile_keys or {}
        self.http = client or httpx.Client(timeout=timeout)
        self.sleep = sleep
        self.stop_grace = stop_grace
        self._warned: set[str] = set()

    def _target(self, profile: str | None) -> tuple[str, str, str]:
        prefix, served = resolve_profile(profile, self.profile_keys)
        if profile and served == "default" and profile not in ("chief", "default") and profile not in self._warned:
            self._warned.add(profile)
            log.warning("no HERMES_API_KEY_%s: tasks for profile %s run on the default profile", profile.upper(),
                        profile, extra={"profile": profile})
        key = self.profile_keys[served] if prefix else self.api_key
        return prefix, key, served

    def _headers(self, key: str, **extra: str) -> dict:
        return {"Authorization": f"Bearer {key}", **extra}

    def submit(self, *, prompt: str, session_id: str, profile: str | None = None, model: str | None = None,
               instructions: str = INSTRUCTIONS, max_wait: float = 300) -> SubmittedRun:
        prefix, key, served = self._target(profile)
        body = {"input": prompt, "session_id": session_id, "instructions": instructions}
        if model:
            body["model"] = model
        url = f"{self.base}{prefix}/v1/runs"
        headers = self._headers(key, **{"Idempotency-Key": session_id})
        deadline = time.monotonic() + max_wait
        delay = 2.0
        while True:
            try:
                resp = self.http.post(url, json=body, headers=headers)
                retry = resp.status_code == 429 or resp.status_code >= 500
                problem = f"HTTP {resp.status_code}: {resp.text[:300]}"
            except httpx.HTTPError as exc:
                retry, problem, resp = True, f"{type(exc).__name__}: {exc}", None
            if resp is not None and resp.status_code in (200, 201, 202):
                try:
                    run_id = resp.json()["run_id"]
                except (ValueError, KeyError, TypeError) as exc:
                    raise HermesError(f"POST /v1/runs: unexpected body {resp.text[:200]!r}") from exc
                return SubmittedRun(run_id=run_id, prefix=prefix, api_key=key, profile=served)
            if not retry or time.monotonic() + delay > deadline:
                raise HermesError(f"POST {prefix}/v1/runs failed: {problem}")
            log.info("hermes busy, retrying", extra={"session_id": session_id, "problem": problem[:120]})
            self.sleep(delay)
            delay = min(delay * 2, 30)

    def status(self, run: SubmittedRun) -> dict:
        try:
            resp = self.http.get(f"{self.base}{run.prefix}/v1/runs/{run.run_id}", headers=self._headers(run.api_key))
        except httpx.HTTPError as exc:
            raise HermesError(f"GET run {run.run_id}: {exc}") from exc
        if resp.status_code != 200:
            raise HermesError(f"GET run {run.run_id}: HTTP {resp.status_code} {resp.text[:200]}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise HermesError(f"GET run {run.run_id}: non-JSON response") from exc
        if not isinstance(data, dict):
            raise HermesError(f"GET run {run.run_id}: unexpected body {resp.text[:200]!r}")
        return data

    def deny_approvals(self, run: SubmittedRun) -> bool:
        """The bench never approves: risky tool calls are denied and the agent has to cope."""
        try:
            resp = self.http.post(f"{self.base}{run.prefix}/v1/runs/{run.run_id}/approval",
                                  json={"choice": "deny", "all": True}, headers=self._headers(run.api_key))
        except httpx.HTTPError as exc:
            log.warning("approval deny failed: %s", exc, extra={"run_id": run.run_id})
            return False
        return resp.status_code == 200

    def stop(self, run: SubmittedRun) -> None:
        try:
            self.http.post(f"{self.base}{run.prefix}/v1/runs/{run.run_id}/stop", headers=self._headers(run.api_key))
        except httpx.HTTPError as exc:
            log.warning("stop failed: %s", exc, extra={"run_id": run.run_id})

    def _follow_events(self, run: SubmittedRun, deadline: float, outcome: RunOutcome) -> bool:
        """Consume the SSE stream until a terminal event. False when the stream was unusable."""
        url = f"{self.base}{run.prefix}/v1/runs/{run.run_id}/events"
        tools = 0
        try:
            with self.http.stream("GET", url, headers=self._headers(run.api_key, Accept="text/event-stream"),
                                  timeout=httpx.Timeout(30, read=45)) as resp:
                if resp.status_code != 200:
                    return False
                for line in resp.iter_lines():
                    if time.monotonic() > deadline:
                        break
                    if not line.startswith("data:"):
                        continue
                    try:
                        event = json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    name = event.get("event") if isinstance(event, dict) else None
                    if name == "tool.started":
                        tools += 1
                    elif name == "approval.request":
                        outcome.approvals_denied += int(self.deny_approvals(run))
                    elif name in TERMINAL_EVENTS:
                        break
        except httpx.HTTPError as exc:
            log.info("event stream ended early, polling instead: %s", exc, extra={"run_id": run.run_id})
            outcome.tool_calls = None
            return False
        outcome.tool_calls = tools
        return True

    def _poll(self, run: SubmittedRun, deadline: float, poll_interval: float, outcome: RunOutcome,
              raise_late: bool) -> dict | None:
        """Poll until a terminal status (returned) or the deadline (None). A status endpoint that is still failing
        at the deadline raises when `raise_late`: an unreachable Hermes is not a slow agent."""
        while True:
            try:
                st = self.status(run)
            except HermesError as exc:
                if time.monotonic() > deadline:
                    if raise_late:
                        raise
                    return None
                log.warning("status poll failed: %s", exc, extra={"run_id": run.run_id})
                st = {}
            if st.get("status") in TERMINAL:
                return st
            if st.get("status") == "waiting_for_approval":
                outcome.approvals_denied += int(self.deny_approvals(run))
            if time.monotonic() > deadline:
                return None
            self.sleep(poll_interval)

    @staticmethod
    def _fill(outcome: RunOutcome, st: dict) -> None:
        outcome.output = st.get("output") or ""
        outcome.usage = st.get("usage") or {}
        outcome.runtime = st.get("runtime") or {}
        outcome.error = st.get("error")

    def wait(self, run: SubmittedRun, timeout: float, poll_interval: float = 2.0) -> RunOutcome:
        """Follow the run to its terminal status. On timeout, /stop it and keep polling for up to `stop_grace` so
        nobody evaluates or deletes the workdir while the agent is still running commands in it."""
        outcome = RunOutcome(status="running")
        deadline = time.monotonic() + timeout
        self._follow_events(run, deadline, outcome)
        st = self._poll(run, deadline, poll_interval, outcome, raise_late=True)
        if st is not None:
            self._fill(outcome, st)
            outcome.status = st["status"]
            return outcome
        self.stop(run)
        st = self._poll(run, time.monotonic() + self.stop_grace, poll_interval, outcome, raise_late=False)
        outcome.status = "timeout"
        if st is None:
            outcome.settled = False
            outcome.error = f"no terminal status after {timeout:.0f}s; still running {self.stop_grace:.0f}s after /stop"
        else:
            self._fill(outcome, st)
            outcome.error = f"no terminal status after {timeout:.0f}s (stopped; run ended {st['status']})"
        return outcome
