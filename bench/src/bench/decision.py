"""Decision Service client (docs/CONTRACTS.md §2): pre-route the bench session with the task's known tenant and
agent, read the run ledger (model/tier/tokens/cost/iterations/repairs/escalations), and report the outcome."""

import logging

import httpx

log = logging.getLogger("bench.decision")


class DecisionError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status  # HTTP status when the service answered, None when it was unreachable


class DecisionClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 30, client: httpx.Client | None = None):
        self.base = base_url.rstrip("/")
        self.api_key = api_key
        self.http = client or httpx.Client(timeout=timeout)

    @property
    def enabled(self) -> bool:
        return bool(self.base and self.api_key)

    def _call(self, method: str, path: str, body: dict | None = None, allow_404: bool = False) -> dict | None:
        try:
            resp = self.http.request(method, self.base + path, json=body,
                                     headers={"Authorization": f"Bearer {self.api_key}"})
        except httpx.HTTPError as exc:
            raise DecisionError(f"{method} {path}: {exc}") from exc
        if allow_404 and resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise DecisionError(f"{method} {path}: HTTP {resp.status_code} {resp.text[:300]}", resp.status_code)
        try:
            return resp.json()
        except ValueError as exc:
            raise DecisionError(f"{method} {path}: non-JSON response") from exc

    def route(self, *, session_id: str, text: str, tenant: str, agent: str, model: str | None = None) -> dict:
        """Create the run before Hermes does, so the ledger has the task's real tenant and agent (and, with `model`,
        a pinned start model). The aios plugin's own /v1/route for this session then returns the existing run
        (CONTRACTS §2.2)."""
        body = {"session_id": session_id, "text": text[:4000], "tenant": tenant, "agent": agent}
        if model:
            body["model"] = model
        return self._call("POST", "/v1/route", body) or {}

    def get_run(self, session_id: str) -> dict | None:
        return self._call("GET", f"/v1/runs/{session_id}", allow_404=True)

    def finish(self, session_id: str, status: str, task_type: str | None = None) -> dict | None:
        body = {"status": status}
        if task_type:
            body["task_type"] = task_type
        try:
            return self._call("POST", f"/v1/runs/{session_id}/finish", body, allow_404=True)
        except DecisionError as exc:
            if task_type and "HTTP 400" in str(exc):  # task type unknown to the running policy: still record it
                log.warning("finish rejected task_type %s, retrying without it", task_type)
                return self._call("POST", f"/v1/runs/{session_id}/finish", {"status": status}, allow_404=True)
            raise
