from conftest import drain

ROUTE = {"run_id": "r1", "session_id": "s1", "agent": "chief", "domain": "engineering", "tenant": "nitro",
         "task_type": "debugging", "complexity": "medium", "model": "tier3-code", "validator": "command",
         "skills": ["coding/debugging"], "token_budget": {"retrieval": 1500}, "budget": {"state": "ok"}}


def test_registers_everything(plugin):
    _, ctx = plugin
    assert set(ctx.hooks) == {"pre_llm_call", "post_api_request", "api_request_error", "pre_tool_call",
                              "post_tool_call", "pre_verify", "on_skill_lifecycle", "on_session_finalize"}
    assert set(ctx.middleware) == {"llm_request"}
    assert "aios.policy" in ctx.sections


def test_first_turn_routes_and_injects_context(plugin, services):
    _, ctx = plugin
    decision, knowledge = services
    decision.routes[("POST", "/v1/route")] = ROUTE
    knowledge.routes[("POST", "/v1/context/compile")] = {"token_estimate": 300, "markdown": "# Contexto\n- regra X"}

    out = ctx.hooks["pre_llm_call"](session_id="s1", user_message="bug no checkout", is_first_turn=True)
    assert "modelo=tier3-code" in out["context"] and "# Contexto" in out["context"]
    assert "kanban_create(assignee='engineering', tenant='nitro')" in out["context"]  # chief told to delegate
    assert decision.body("/v1/route")["agent"] == "chief"
    compile_req = knowledge.body("/v1/context/compile")
    assert compile_req["tenant"] == "nitro" and compile_req["budget_tokens"] == 1500 and compile_req["domain"] == "engineering"

    # second turn: no re-route, no re-inject
    assert ctx.hooks["pre_llm_call"](session_id="s1", user_message="e agora?") is None
    assert decision.paths().count("/v1/route") == 1


def test_services_down_fail_open(plugin, monkeypatch):
    mod, ctx = plugin
    monkeypatch.setattr(mod._decision, "base_url", "http://127.0.0.1:1")
    monkeypatch.setattr(mod._knowledge, "base_url", "http://127.0.0.1:1")
    assert ctx.hooks["pre_llm_call"](session_id="s2", user_message="oi") is None
    assert ctx.middleware["llm_request"](request={"model": "tier3-code", "messages": []}, session_id="s2") is None


def test_middleware_swaps_model(plugin, services):
    _, ctx = plugin
    decision, _ = services
    decision.routes[("POST", "/v1/models/resolve")] = {"model": "tier4-pro", "source": "run", "tier": 4}
    req = {"model": "tier3-code", "messages": [{"role": "user", "content": "x"}], "tools": []}
    out = ctx.middleware["llm_request"](request=req, session_id="s3", model="tier3-code")
    assert out["request"]["model"] == "tier4-pro" and out["request"]["messages"] == req["messages"]
    assert req["model"] == "tier3-code"  # original untouched (complete replacement payload)
    decision.routes[("POST", "/v1/models/resolve")] = {"model": "tier3-code", "source": "run"}
    assert ctx.middleware["llm_request"](request=req, session_id="s3") is None  # unchanged -> no rewrite


def test_usage_reports_total_input(plugin, services):
    mod, ctx = plugin
    decision, _ = services
    decision.routes[("POST", "/v1/models/resolve")] = {"model": "tier4-pro"}
    decision.routes[("POST", "/v1/usage")] = {"cost_usd": 0.1, "budget": {"state": "exhausted"}}
    ctx.middleware["llm_request"](request={"model": "tier3-code"}, session_id="s4")
    ctx.hooks["on_skill_lifecycle"](skill_name="coding/debugging", session_id="s4", action="view")
    usage = {"input_tokens": 800, "cache_read_tokens": 200, "cache_write_tokens": 0, "output_tokens": 50,
             "prompt_tokens": 1000, "total_tokens": 1050}
    ctx.hooks["post_api_request"](session_id="s4", api_request_id="a1", usage=usage, model="tier3-code", api_duration=1.5)
    drain()
    body = decision.body("/v1/usage")
    assert body["input_tokens"] == 1000 and body["cache_read_tokens"] == 200 and body["output_tokens"] == 50
    assert body["model"] == "tier4-pro" and body["skills"] == ["coding/debugging"] and body["latency_ms"] == 1500
    # budget exhausted -> tools blocked except wrap-up
    assert ctx.hooks["pre_tool_call"](tool_name="web_search", args={}, session_id="s4")["action"] == "block"
    assert ctx.hooks["pre_tool_call"](tool_name="kanban_complete", args={}, session_id="s4") is None


def test_pre_tool_call_enforces_policy(plugin, services, monkeypatch):
    _, ctx = plugin
    decision, _ = services
    decision.routes[("POST", "/v1/route")] = {**ROUTE, "agent": "engineering", "tenant": "nitro"}
    monkeypatch.setenv("HERMES_PROFILE", "engineering")
    ctx.hooks["pre_llm_call"](session_id="s5", user_message="bug")
    block = ctx.hooks["pre_tool_call"](tool_name="mcp__knowledge__knowledge_search",
                                       args={"tenant": "pessoal", "query": "x"}, session_id="s5")
    assert block["action"] == "block" and "isolamento" in block["message"]
    assert ctx.hooks["pre_tool_call"](tool_name="terminal", args={"command": "go test ./..."}, session_id="s5") is None
    assert ctx.hooks["pre_tool_call"](tool_name="read_file", args={"path": ".env"}, session_id="s5")["action"] == "block"


def test_pre_verify_gate_loop(plugin, services, monkeypatch):
    mod, ctx = plugin
    decision, _ = services
    results = iter([{"exit_code": 1, "output_tail": "FAIL TestX", "command": "go test ./..."},
                    {"exit_code": 0, "output_tail": "ok", "command": "go test ./..."}])
    monkeypatch.setattr(mod.checks, "run_check", lambda path: next(results))
    gates = iter([{"action": "repair", "message": "corrija TestX", "run": {"model": "tier3-code"}},
                  {"action": "done", "message": "ok", "run": {"model": "tier3-code"}}])
    decision.routes[("POST", "/v1/gate")] = lambda body: next(gates)

    first = ctx.hooks["pre_verify"](session_id="s6", coding=True, attempt=0, changed_paths=["/workspace/x/a.go"])
    assert first == {"action": "continue", "message": "[aios gate: repair] corrija TestX"}
    assert decision.body("/v1/gate")["evidence"]["validation"] == "fail"
    assert ctx.hooks["pre_verify"](session_id="s6", coding=True, attempt=1, changed_paths=["/workspace/x/a.go"]) is None
    assert decision.body("/v1/gate")["evidence"]["tests"]["exit_code"] == 0
    assert ctx.hooks["pre_verify"](session_id="s6", coding=False, attempt=0, changed_paths=["a"]) is None


def test_gate_fail_stops_once(plugin, services, monkeypatch):
    mod, ctx = plugin
    decision, _ = services
    monkeypatch.setattr(mod.checks, "run_check", lambda path: {"exit_code": 1, "output_tail": "x"})
    decision.routes[("POST", "/v1/gate")] = {"action": "fail", "message": "Pare: budget exhausted"}
    out = ctx.hooks["pre_verify"](session_id="s7", coding=True, attempt=0, changed_paths=["a.go"])
    assert out["action"] == "continue" and "Pare" in out["message"]
    assert ctx.hooks["pre_verify"](session_id="s7", coding=True, attempt=1, changed_paths=["a.go"]) is None  # no loop


def test_finalize_and_kanban_complete(plugin, services):
    _, ctx = plugin
    decision, _ = services
    decision.routes[("POST", "/v1/route")] = ROUTE
    decision.routes[("POST", "/v1/runs/s8/finish")] = {"status": "succeeded"}
    ctx.hooks["pre_llm_call"](session_id="s8", user_message="x")
    ctx.hooks["post_tool_call"](tool_name="kanban_complete", args={}, result="{}", status="ok", session_id="s8")
    ctx.hooks["on_session_finalize"](session_id="s8")
    drain()
    finishes = [b for _, p, b, _ in decision.calls if p == "/v1/runs/s8/finish"]
    assert finishes and all(b["status"] == "succeeded" for b in finishes)


def test_aios_task_check_counts_as_a_test():
    from aios import checks

    assert checks.looks_like_test("aios-task-check t-123") and checks.looks_like_test("python3 -m unittest -q")


def test_test_results_are_remembered(plugin, services, monkeypatch):
    mod, ctx = plugin
    decision, _ = services
    monkeypatch.setattr(mod.checks, "run_check", lambda path: None)  # no sandbox: use what the agent ran
    decision.routes[("POST", "/v1/gate")] = {"action": "done", "message": "ok"}
    ctx.hooks["post_tool_call"](tool_name="terminal", args={"command": "go test ./..."},
                                result='{"output": "--- FAIL: TestA", "exit_code": 1}', status="ok", session_id="s9")
    ctx.hooks["pre_verify"](session_id="s9", coding=True, attempt=0, changed_paths=["a.go"])
    ev = decision.body("/v1/gate")["evidence"]
    assert ev["validation"] == "fail" and ev["tests"]["exit_code"] == 1


def test_tool_search_bridge_is_unwrapped(plugin, services, monkeypatch):
    _, ctx = plugin
    decision, _ = services
    decision.routes[("POST", "/v1/route")] = {**ROUTE, "agent": "engineering", "tenant": "nitro"}
    monkeypatch.setenv("HERMES_PROFILE", "engineering")
    ctx.hooks["pre_llm_call"](session_id="s10", user_message="bug")
    wrapped = {"calls": [{"name": "web_search", "arguments": {"query": "x"}},
                         {"name": "mcp__knowledge__knowledge_search", "arguments": {"tenant": "pessoal", "query": "x"}}]}
    block = ctx.hooks["pre_tool_call"](tool_name="tool_call", args=wrapped, session_id="s10")
    assert block and block["action"] == "block" and "isolamento" in block["message"]
    ok = {"calls": [{"name": "mcp__knowledge__knowledge_search", "arguments": {"tenant": "nitro", "query": "x"}}]}
    assert ctx.hooks["pre_tool_call"](tool_name="tool_call", args=ok, session_id="s10") is None


def test_cron_tenant_tag_routes_even_behind_skill_bodies(plugin, services):
    _, ctx = plugin
    decision, knowledge = services
    decision.routes[("POST", "/v1/route")] = {**ROUTE, "tenant": "pessoal", "domain": "chief"}
    knowledge.routes[("POST", "/v1/context/compile")] = {"token_estimate": 0, "markdown": ""}
    message = "[skill daily_review]\n" + "corpo da skill " * 600 + "\nRevisão da manhã. Tenant desta execução: **pessoal**."
    ctx.hooks["pre_llm_call"](session_id="c1", user_message=message, is_first_turn=True)
    assert decision.body("/v1/route")["tenant"] == "pessoal"
    assert knowledge.body("/v1/context/compile")["tenant"] == "pessoal"


def test_worker_tenant_beats_the_tag(plugin, services, monkeypatch):
    _, ctx = plugin
    decision, _ = services
    decision.routes[("POST", "/v1/route")] = ROUTE
    monkeypatch.setenv("HERMES_TENANT", "nitro")
    ctx.hooks["pre_llm_call"](session_id="c2", user_message="Tenant desta execução: **pessoal**")
    assert decision.body("/v1/route")["tenant"] == "nitro"


def test_bench_sessions_cannot_leave_traces(plugin, services):
    _, ctx = plugin
    for tool in ("memory", "mcp__knowledge__memory_save", "mcp__knowledge__ingest_note", "kanban_create",
                 "cronjob_manage", "send_message"):
        out = ctx.hooks["pre_tool_call"](tool_name=tool, args={}, session_id="bench-simple-x-1-20261007000000")
        assert out and out["action"] == "block" and "bench" in out["message"], tool
    wrapped = {"calls": [{"name": "memory", "arguments": {"action": "add"}}]}
    assert ctx.hooks["pre_tool_call"](tool_name="tool_call", args=wrapped, session_id="bench-y")["action"] == "block"
    assert ctx.hooks["pre_tool_call"](tool_name="memory", args={}, session_id="s-normal") is None
    assert ctx.hooks["pre_tool_call"](tool_name="mcp__knowledge__knowledge_search", args={}, session_id="bench-z") is None


def test_edge_key_is_scoped_to_the_worker_tenant(services, monkeypatch):
    import importlib
    import sys

    from conftest import FakeCtx

    def load(**env):
        for k in ("EDGE_API_KEY", "EDGE_TENANT_KEYS", "HERMES_TENANT", "HERMES_KANBAN_TASK"):
            monkeypatch.delenv(k, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        for name in [m for m in sys.modules if m == "aios" or m.startswith("aios.")]:
            del sys.modules[name]
        importlib.import_module("aios").register(FakeCtx())
        import os
        return os.environ.get("EDGE_API_KEY"), os.environ.get("EDGE_TENANT_KEYS")

    keys = "nitro:nitro-key-0000000000,pessoal:pessoal-key-000000000"
    # Kanban worker: only its tenant's key, and the map is dropped
    assert load(EDGE_TENANT_KEYS=keys, HERMES_TENANT="pessoal", HERMES_KANBAN_TASK="t1") == ("pessoal-key-000000000", None)
    # gateway (chief, no tenant): no edge key, keeps the map for the workers it spawns
    assert load(EDGE_TENANT_KEYS=keys, EDGE_API_KEY="admin") == (None, keys)
    # no tenant map configured: EDGE_API_KEY passes as is
    assert load(EDGE_API_KEY="admin") == ("admin", None)
