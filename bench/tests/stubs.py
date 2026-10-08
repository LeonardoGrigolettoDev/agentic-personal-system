"""In-process stand-ins for the Hermes API server (Runs API) and the Decision Service, speaking the same wire
format the bench client was verified against (see bench/hermes.py and docs/CONTRACTS.md §2)."""

import json
import re
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@contextmanager
def serve(handler_cls, state):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    server.state = state
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


class _Base(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep pytest output clean
        pass

    @property
    def state(self):
        return self.server.state

    def body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def send_json(self, code: int, payload) -> None:
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


# ---------------------------------------------------------------- Hermes

@dataclass
class HermesState:
    keys: dict[str, str]  # URL prefix ("" or "/p/<profile>") -> API key
    answer: str = "ok"
    on_submit: object = None  # callable(body) run before the run is created
    throttle: int = 0  # number of 429s before accepting
    submit_error: int | None = None
    events_status: int = 200
    ask_approval: bool = False
    never_finish: bool = False
    ignore_stop: bool = False  # a stopped run stays "running" (agent wedged in a tool call)
    final_status: str = "completed"  # terminal status of finished runs: failed | interrupted | cancelled too
    final_error: str | None = None  # `error` on the terminal status (provider/runtime failure)
    usage: dict = field(default_factory=lambda: {"input_tokens": 900, "output_tokens": 120, "total_tokens": 1020,
                                                  "cache_read_tokens": 0, "cache_write_tokens": 0})
    runs: dict = field(default_factory=dict)
    submits: list = field(default_factory=list)  # (path, headers, body)
    approvals: list = field(default_factory=list)
    stops: list = field(default_factory=list)
    approved: threading.Event = field(default_factory=threading.Event)


_RUN_PATH = re.compile(r"^(?P<prefix>(/p/[a-z]+)?)/v1/runs(/(?P<id>run_[0-9a-f]+)(?P<action>/events|/approval|/stop)?)?$")


class HermesHandler(_Base):
    def _route(self):
        m = _RUN_PATH.match(self.path)
        if not m:
            self.send_json(404, {"error": {"message": "not found"}})
            return None
        prefix = m.group("prefix") or ""
        if self.headers.get("Authorization") != f"Bearer {self.state.keys.get(prefix)}":
            self.send_json(401, {"error": {"message": "unauthorized"}})
            return None
        return m

    def do_POST(self):
        m = self._route()
        if not m:
            return
        st = self.state
        if m.group("id") is None:
            body = self.body()
            st.submits.append((self.path, dict(self.headers), body))
            if st.throttle > 0:
                st.throttle -= 1
                self.send_json(429, {"error": {"message": "Too many concurrent runs (max 1)"}})
                return
            if st.submit_error:
                self.send_json(st.submit_error, {"error": {"message": "boom"}})
                return
            if st.on_submit:
                st.on_submit(body)
            run_id = f"run_{uuid.uuid4().hex}"
            status = "running" if st.never_finish else "waiting_for_approval" if st.ask_approval else "done"
            st.runs[run_id] = {"object": "hermes.run", "run_id": run_id, "status": status,
                               "session_id": body.get("session_id"), "model": body.get("model", "hermes-agent")}
            if status == "done":
                self._complete(run_id, body)
            st.runs[run_id]["_body"] = body
            self.send_json(202, {"run_id": run_id, "status": "started", "replayed": False})
        elif m.group("action") == "/approval":
            st.approvals.append(self.body())
            run = st.runs[m.group("id")]
            self._complete(m.group("id"), run["_body"])
            st.approved.set()
            self.send_json(200, {"object": "hermes.run.approval_response", "run_id": run["run_id"], "resolved": 1})
        elif m.group("action") == "/stop":
            st.stops.append(m.group("id"))
            if not st.ignore_stop:
                st.runs[m.group("id")]["status"] = "cancelled"
            self.send_json(200, {"run_id": m.group("id"), "status": "stopping"})

    def _complete(self, run_id, body):
        st = self.state
        if st.final_status != "completed":  # Hermes' _finish(status, error=...) on failures carries no output/usage
            st.runs[run_id].update(status=st.final_status, completed=False,
                                   **({"error": st.final_error} if st.final_error else {}))
            return
        st.runs[run_id].update(status="completed", output=st.answer, usage=st.usage,
                               runtime={"provider": "custom", "model": body.get("model") or "tier3-code",
                                        "route_source": "global"})

    def do_GET(self):
        m = self._route()
        if not m:
            return
        run = self.state.runs.get(m.group("id"))
        if run is None:
            self.send_json(404, {"error": {"message": "Run not found"}})
        elif m.group("action") == "/events":
            self._events(run)
        else:
            self.send_json(200, {k: v for k, v in run.items() if not k.startswith("_")})

    def _events(self, run):
        if self.state.events_status != 200:
            self.send_json(self.state.events_status, {"error": {"message": "no stream"}})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        def frame(event, **fields):
            data = {"event": event, "run_id": run["run_id"], "timestamp": 0, **fields}
            self.wfile.write(f"data: {json.dumps(data)}\n\n".encode())
            self.wfile.flush()

        self.wfile.write(b": keepalive\n\n")
        frame("tool.started", tool="terminal", preview="go test ./...")
        frame("tool.completed", tool="terminal", duration=0.1, error=False, preview="ok")
        if run["status"] == "waiting_for_approval":
            frame("approval.request", command="rm -rf build", choices=["once", "session", "always", "deny"])
            self.state.approved.wait(5)
        frame("tool.started", tool="read_file", preview="paginacao.py")
        frame(f"run.{run['status']}", output=run.get("output", ""))
        self.wfile.write(b": stream closed\n\n")
        self.wfile.flush()


# ---------------------------------------------------------------- Decision Service

@dataclass
class DecisionState:
    api_key: str = "dk"
    ledger: dict = field(default_factory=dict)  # values merged into every run
    down: bool = False
    routes: list = field(default_factory=list)
    finishes: list = field(default_factory=list)  # (session_id, body)
    runs: dict = field(default_factory=dict)
    get_updates: list = field(default_factory=list)  # merged into the run on successive GETs (late /v1/usage)
    gets: int = 0
    honor_model: bool = True  # False = a Decision Service that predates pinned start models
    reject_model: bool = False  # True = the pinned model is above the agent's max_tier (400)


class DecisionHandler(_Base):
    def _auth(self) -> bool:
        if self.state.down:
            self.send_json(503, {"error": "policy/ledger not configured"})
            return False
        if self.headers.get("Authorization") != f"Bearer {self.state.api_key}":
            self.send_json(401, {"error": "unauthorized"})
            return False
        return True

    def do_POST(self):
        if not self._auth():
            return
        body = self.body()
        if self.path == "/v1/route":
            self.state.routes.append(body)
            if body.get("model") and self.state.reject_model:
                self.send_json(400, {"error": f"model {body['model']!r} is above agent max_tier"})
                return
            pinned = {}
            if body.get("model") and self.state.honor_model:  # the ledger's `model` (later escalation) still wins
                pinned = {"start_model": body["model"], "model": self.state.ledger.get("model", body["model"])}
            run = self.state.runs.setdefault(body["session_id"], {
                "run_id": str(uuid.uuid4()), "session_id": body["session_id"], "agent": body.get("agent", "chief"),
                "tenant": body.get("tenant"), "status": "running", **self.state.ledger, **pinned})
            self.send_json(200, {**run, "existing": False})
            return
        m = re.match(r"^/v1/runs/(?P<sid>[^/]+)/finish$", self.path)
        if m:
            sid = m.group("sid")
            self.state.finishes.append((sid, body))
            if sid not in self.state.runs:
                self.send_json(404, {"error": "no run for session"})
                return
            self.state.runs[sid]["status"] = body["status"]
            self.send_json(200, self.state.runs[sid])
            return
        self.send_json(404, {"error": "not found"})

    def do_GET(self):
        if not self._auth():
            return
        m = re.match(r"^/v1/runs/(?P<sid>[^/]+)$", self.path)
        run = self.state.runs.get(m.group("sid")) if m else None
        if run is not None:
            self.state.gets += 1
            if self.state.get_updates:
                run.update(self.state.get_updates.pop(0))
        if run is None:
            self.send_json(404, {"error": "no run for session"})
        else:
            self.send_json(200, run)
