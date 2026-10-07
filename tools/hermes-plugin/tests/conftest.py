"""Fixtures: a fake Hermes plugin context and stub decision/knowledge HTTP services."""

import importlib
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "tools" / "hermes-plugin"))


class FakeCtx:
    """Records what the plugin registers, like hermes_cli.plugins.PluginContext."""

    def __init__(self):
        self.hooks, self.middleware, self.sections = {}, {}, {}

    def register_hook(self, name, fn):
        self.hooks[name] = fn

    def register_middleware(self, kind, fn):
        self.middleware[kind] = fn

    def register_system_prompt_section(self, id, content, **_):
        assert len(content) <= 4000
        self.sections[id] = content


class Stub:
    """Tiny HTTP service: routes[(method, path)] -> dict | callable(body) -> dict; records requests."""

    def __init__(self):
        self.routes, self.calls = {}, []
        stub = self

        class H(BaseHTTPRequestHandler):
            def _do(self, method):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
                stub.calls.append((method, self.path, body, dict(self.headers)))
                handler = stub.routes.get((method, self.path))
                if handler is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                out = json.dumps(handler(body) if callable(handler) else handler).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def do_POST(self):
                self._do("POST")

            def do_GET(self):
                self._do("GET")

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def paths(self):
        return [p for _, p, _, _ in self.calls]

    def body(self, path):
        return next(b for _, p, b, _ in reversed(self.calls) if p == path)


@pytest.fixture
def services(monkeypatch):
    decision, knowledge = Stub(), Stub()
    monkeypatch.setenv("AIOS_DECISION_URL", decision.url)
    monkeypatch.setenv("DECISION_API_KEY", "dk")
    monkeypatch.setenv("AIOS_KNOWLEDGE_URL", knowledge.url)
    monkeypatch.setenv("KNOWLEDGE_API_KEY", "kk")
    monkeypatch.setenv("AIOS_AGENTS_DIR", str(REPO / "agents"))
    for var in ("HERMES_PROFILE", "AIOS_AGENT", "HERMES_TENANT", "AIOS_DEFAULT_TENANT", "HERMES_KANBAN_TASK",
                "TERMINAL_SSH_HOST", "HERMES_WEBHOOK_SECRET"):
        monkeypatch.delenv(var, raising=False)
    yield decision, knowledge
    decision.server.shutdown()
    knowledge.server.shutdown()


@pytest.fixture
def plugin(services):
    """Fresh plugin module per test (module-level state), registered on a fake context."""
    for name in [m for m in sys.modules if m == "aios" or m.startswith("aios.")]:
        del sys.modules[name]
    mod = importlib.import_module("aios")
    ctx = FakeCtx()
    mod.register(ctx)
    return mod, ctx


def drain():
    """Wait for fire-and-forget background calls."""
    from aios import checks

    checks.flush(timeout=5)
