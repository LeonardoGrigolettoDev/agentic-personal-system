"""Validation evidence for the gate: run `aios-check` in the sandbox (same SSH target as Hermes' terminal
backend), recognise test commands the agent ran itself, and emit signed events to the Decision Service."""

import hashlib
import hmac
import json
import logging
import os
import re
import shlex
import subprocess
import threading
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor, wait

log = logging.getLogger("aios.checks")

_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="aios")
_pending: set[Future] = set()
_pending_lock = threading.Lock()
TEST_COMMAND = re.compile(r"\b(go test|pytest|uv run pytest|npm (run )?test|pnpm (run )?test|yarn test|cargo test|"
                          r"make (test|check)|vitest|jest|aios-check)\b")
EXIT_CODE = re.compile(r"(?:exit[_ ]code|exit status|Exit code)[\"':= ]+(-?\d+)", re.IGNORECASE)


def background(fn) -> None:
    """Fire-and-forget off the agent loop (usage/events must not add latency)."""
    try:
        fut = _pool.submit(fn)
    except RuntimeError:  # interpreter shutting down
        threading.Thread(target=fn, daemon=True).start()
        return
    with _pending_lock:
        _pending.add(fut)
    fut.add_done_callback(lambda f: _discard(f))


def _discard(fut: Future) -> None:
    with _pending_lock:
        _pending.discard(fut)


def flush(timeout: float = 10) -> None:
    """Wait for queued background calls (tests, shutdown)."""
    with _pending_lock:
        pending = list(_pending)
    wait(pending, timeout=timeout)


def looks_like_test(command) -> bool:
    return isinstance(command, str) and bool(TEST_COMMAND.search(command))


def parse_terminal_result(command: str, result) -> dict:
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
    exit_code = 0
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            exit_code = int(data.get("exit_code", data.get("returncode", 0)) or 0)
            text = str(data.get("output") or data.get("stdout") or text)
    except (ValueError, TypeError):
        if m := EXIT_CODE.search(text):
            exit_code = int(m.group(1))
    return {"exit_code": exit_code, "output_tail": text[-4000:], "command": command[:500]}


def run_check(changed_path: str | None) -> dict | None:
    """Run the sandbox validator for the repo containing changed_path. None when no sandbox is configured."""
    host = os.environ.get("TERMINAL_SSH_HOST")
    if not host or not changed_path:
        return None
    target = f"{os.environ.get('TERMINAL_SSH_USER', 'agent')}@{host}"
    cmd = ["ssh", "-p", os.environ.get("TERMINAL_SSH_PORT", "22"), "-o", "BatchMode=yes",
           "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10"]
    if key := os.environ.get("TERMINAL_SSH_KEY"):
        cmd += ["-i", key]
    cmd += [target, "aios-check --json " + shlex.quote(changed_path)]
    timeout = int(os.environ.get("AIOS_CHECK_TIMEOUT", "600"))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"exit_code": 124, "output_tail": f"aios-check timed out after {timeout}s", "command": "aios-check"}
    except OSError as exc:
        log.warning("aios-check could not run: %s", exc)
        return None
    try:
        out = json.loads(proc.stdout.strip().splitlines()[-1])
        if isinstance(out, dict) and "exit_code" in out:
            if out.get("skipped"):
                return None  # no test command detected for this repo
            return out
    except (ValueError, IndexError):
        pass
    log.warning("aios-check gave no JSON (rc=%s): %s", proc.returncode, (proc.stderr or proc.stdout)[-300:])
    return None


def emit_event(event_type: str, payload: dict) -> None:
    """POST a signed event to decision /v1/hermes-events (X-Hermes-Signature-256)."""
    secret = os.environ.get("HERMES_WEBHOOK_SECRET")
    base = os.environ.get("AIOS_DECISION_URL", "http://decision:8080")
    if not secret:
        return
    body = json.dumps({"event": event_type, **payload}, ensure_ascii=False, default=str).encode()
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    req = urllib.request.Request(base.rstrip("/") + "/v1/hermes-events", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "X-Hermes-Signature-256": sig})
    try:
        urllib.request.urlopen(req, timeout=5).close()
    except OSError as exc:
        log.debug("event %s not delivered: %s", event_type, exc)
