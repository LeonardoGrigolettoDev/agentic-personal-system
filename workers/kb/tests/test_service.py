"""Offline tests for the knowledge service: auth, health, MCP handshake, validation paths."""

import pytest
from fastapi.testclient import TestClient

from kb.config import Settings
from kb.service import create_app

KEY = "test-key"
SETTINGS = Settings(
    database_url="postgresql://nobody@127.0.0.1:1/none", litellm_base_url="http://127.0.0.1:1", litellm_key=None,
    embed_model="embed-local", embed_model_name="qwen3-embedding:0.6b", embed_dim=4, embed_batch_size=4,
    embed_timeout=1,
)


class FakePool:
    def connection(self, timeout=None):
        raise RuntimeError("no database in unit tests")


class FakeEmbedder:
    def embed_query(self, q):
        return [0.1, 0.2, 0.3, 0.4]

    def embed_documents(self, texts):
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_TENANTS", "pessoal,shared")
    app = create_app(SETTINGS, pool=FakePool(), embedder=FakeEmbedder(), api_key=KEY)
    with TestClient(app) as c:
        yield c


AUTH = {"Authorization": f"Bearer {KEY}"}


def test_health_is_open(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").status_code == 503  # no database


def test_requires_bearer(client):
    for headers in ({}, {"Authorization": "Bearer nope"}):
        assert client.post("/v1/search", json={"tenant": "pessoal", "query": "x"}, headers=headers).status_code == 401
    assert client.post("/mcp/", json={}, headers={}).status_code == 401


def test_tenant_ceiling(client):
    r = client.post("/v1/search", json={"tenant": "nitro", "query": "x"}, headers=AUTH)
    assert r.status_code == 403
    r = client.post("/v1/search", json={"tenant": "bogus", "query": "x"}, headers=AUTH)
    assert r.status_code == 400


def test_body_validation(client):
    assert client.post("/v1/context/compile", json={"tenant": "pessoal", "task": ""}, headers=AUTH).status_code == 422
    r = client.post("/v1/memories", json={"tenant": "pessoal", "scope": "domain", "kind": "fact", "content": "x"},
                    headers=AUTH)
    assert r.status_code == 400 and "domain" in r.json()["detail"]
    r = client.post("/v1/ingest", json={"tenant": "pessoal", "source_uri": "/home/x/a.md", "content": "y"},
                    headers=AUTH)
    assert r.status_code == 400 and "abstract" in r.json()["detail"]


def _mcp(client, method, params, id_=1):
    return client.post(
        "/mcp/",
        json={"jsonrpc": "2.0", "id": id_, "method": method, "params": params},
        headers={**AUTH, "Accept": "application/json, text/event-stream", "Content-Type": "application/json",
                 "MCP-Protocol-Version": "2025-06-18"},
    )


def test_mcp_lists_tools(client):
    init = _mcp(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                       "clientInfo": {"name": "test", "version": "0"}})
    assert init.status_code == 200, init.text
    tools = _mcp(client, "tools/list", {}, 2)
    assert tools.status_code == 200, tools.text
    names = {t["name"] for t in tools.json()["result"]["tools"]}
    assert names == {"knowledge_search", "compile_context", "memory_save", "memory_search", "ingest_note",
                     "project_list", "project_upsert"}


def test_mcp_tool_error_is_readable(client):
    _mcp(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                "clientInfo": {"name": "test", "version": "0"}})
    r = _mcp(client, "tools/call", {"name": "knowledge_search", "arguments": {"tenant": "nitro", "query": "x"}}, 3)
    result = r.json()["result"]
    assert result["isError"] is True
    assert "not served" in result["content"][0]["text"]


def test_project_validation_needs_no_database(client):
    bad = [({"slug": "Bad Slug", "name": "x"}, "slug"), ({"slug": "app", "name": "x", "repository": "/home/me/app"}, "host path"),
           ({"slug": "app", "name": "x", "domain": "nope"}, "domain"), ({"slug": "app", "name": "x", "status": "done"}, "status")]
    for body, msg in bad:
        r = client.post("/v1/projects", json={"tenant": "pessoal", **body}, headers=AUTH)
        assert r.status_code == 400 and msg in r.json()["detail"], (body, r.text)
    r = client.post("/v1/projects", json={"tenant": "nitro", "slug": "app", "name": "x"}, headers=AUTH)
    assert r.status_code == 403  # tenant ceiling applies to projects too
