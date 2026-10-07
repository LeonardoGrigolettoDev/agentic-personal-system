"""aios — Hermes plugin that wires the runtime to the AI Agent OS control services (docs/CONTRACTS.md §3).

Hermes keeps running agents, sessions, skills, cron, hooks, tools and execution. At the gates this
plugin asks the Decision Service (Go) and the knowledge service:

  pre_llm_call (1st turn)  -> decision /v1/route + knowledge /v1/context/compile -> injected context
  llm_request middleware   -> decision /v1/models/resolve: per-call model (routing + escalation)
  post_api_request         -> decision /v1/usage (cost ledger, budget state)
  pre_tool_call            -> local permissions (agent.yaml), secret/destructive guards, tenant isolation, budget
  pre_verify (code edited) -> sandbox aios-check -> decision /v1/gate -> continue (repair/escalate) or finish
  on_session_finalize      -> decision /v1/runs/{id}/finish

Every remote call is fail-open for availability (a down service never wedges the agent), except
the local safety guards, which always apply.
"""

import logging
import os
from pathlib import Path

from . import checks, client, permissions
from .state import Sessions

log = logging.getLogger("aios")

POLICY_SECTION = """## AI Agent OS — política operacional
- Determinístico primeiro: scripts, testes, SQL e ferramentas antes de raciocinar longamente.
- Contexto enxuto: use `mcp__knowledge__knowledge_search` / `compile_context` em vez de pedir tudo ao usuário.
- Sempre passe o `tenant` da tarefa às ferramentas de conhecimento (nitro = trabalho, pessoal = vida pessoal,
  shared = global). Nunca misture contexto de tenants diferentes.
- Delegue trabalho de domínio a outro agente com `kanban_create(assignee=<engineering|finance|projects|personal|learning>,
  tenant=<tenant>)` quando o roteamento indicar outro domínio.
- O modelo é escolhido pelo Decision Service; não peça para trocar de modelo. Escalonamento acontece por evidência.
- Respeite o orçamento da tarefa. Ao receber "orçamento esgotado", pare e entregue um relatório.
- Ações destrutivas, gastos e mensagens externas exigem aprovação humana explícita.
- Salve em memória (`memory_save`) apenas o que tem valor futuro: decisões, preferências, regras, fatos duráveis."""

_sessions = Sessions(max_sessions=int(os.environ.get("AIOS_MAX_SESSIONS", "512")))
_agents: dict[str, permissions.AgentPolicy] = {}
_decision = client.decision()
_knowledge = client.knowledge()

# Tools that stay available after the budget is exhausted so the agent can wrap up cleanly.
WRAP_UP_TOOLS = {"kanban_complete", "kanban_block", "kanban_comment", "clarify", "send_message", "memory", "todo"}


def register(ctx) -> None:
    global _agents
    _agents = permissions.load_agents(Path(os.environ.get("AIOS_AGENTS_DIR", "/opt/aios/agents")))
    ctx.register_system_prompt_section("aios.policy", POLICY_SECTION)
    ctx.register_middleware("llm_request", on_llm_request)
    for name, fn in (("pre_llm_call", on_pre_llm_call), ("post_api_request", on_post_api_request),
                     ("api_request_error", on_api_request_error), ("pre_tool_call", on_pre_tool_call),
                     ("post_tool_call", on_post_tool_call), ("pre_verify", on_pre_verify),
                     ("on_skill_lifecycle", on_skill_lifecycle), ("on_session_finalize", on_session_finalize)):
        ctx.register_hook(name, fn)
    log.info("aios plugin registered: profile=%s agents=%d decision=%s knowledge=%s", permissions.profile_name(),
             len(_agents), _decision.enabled, _knowledge.enabled)


def _agent() -> permissions.AgentPolicy | None:
    return _agents.get(permissions.profile_name())


# ---------------------------------------------------------------- routing + context (first turn)

def on_pre_llm_call(session_id: str = "", user_message=None, is_first_turn: bool = False, **_):
    if not session_id:
        return None
    s = _sessions.get(session_id)
    text = user_message if isinstance(user_message, str) else str(user_message or "")
    parts: list[str] = []

    if not s.routed and _decision.enabled:
        body = {"session_id": session_id, "text": text[:4000], "agent": permissions.profile_name()}
        if tenant := os.environ.get("HERMES_TENANT") or os.environ.get("AIOS_DEFAULT_TENANT"):
            body["tenant"] = tenant
        if task := os.environ.get("HERMES_KANBAN_TASK"):
            body["task_id"] = task
        try:
            run = _decision.post("/v1/route", body)
            s.routed, s.run = True, run
            s.tenant = run.get("tenant") or body.get("tenant") or "shared"
            s.model = run.get("model") or s.model
            s.budget_state = (run.get("budget") or {}).get("state", "ok")
            parts.append(_route_note(run))
        except client.ServiceError as exc:
            log.warning("route failed (continuing with defaults): %s", exc)

    if not s.context_injected and _knowledge.enabled and text.strip():
        run = s.run or {}
        budget = ((run.get("token_budget") or {}).get("retrieval")) or 3000
        try:
            ctx = _knowledge.post("/v1/context/compile", {
                "task": text[:8000], "tenant": s.tenant or "shared", "domain": run.get("domain") or None,
                "budget_tokens": int(budget), "format": "markdown"})
            s.context_injected = True
            if ctx.get("token_estimate", 0) > 40:
                parts.append(ctx.get("markdown", ""))
        except client.ServiceError as exc:
            log.warning("context compile failed: %s", exc)

    if s.budget_state == "exhausted":
        parts.append("⚠️ Orçamento desta tarefa esgotado: não inicie trabalho novo; entregue um relatório do estado atual.")
    return {"context": "\n\n".join(p for p in parts if p)} if parts else None


def _route_note(run: dict) -> str:
    me = permissions.profile_name()
    lines = [f"[aios] agente={run.get('agent')} domínio={run.get('domain')} tenant={run.get('tenant') or '-'} "
             f"tipo={run.get('task_type')} complexidade={run.get('complexity')} modelo={run.get('model')}"]
    if skills := run.get("skills"):
        lines.append("Skills sugeridas: " + ", ".join(skills))
    if (validator := run.get("validator")) and validator != "none":
        lines.append(f"Validação exigida antes de concluir: {validator}.")
    if run.get("needs_research"):
        lines.append("Pesquise (web/documentação) antes de executar.")
    if run.get("needs_confirmation"):
        lines.append("Esta tarefa exige confirmação humana antes de qualquer ação destrutiva, gasto ou envio externo.")
    domain_agent = run.get("domain")
    if me == "chief" and domain_agent and domain_agent not in ("chief", None):
        lines.append(f"Domínio '{domain_agent}': se for trabalho substancial, delegue com "
                     f"kanban_create(assignee='{domain_agent}', tenant='{run.get('tenant') or 'shared'}').")
    return "\n".join(lines)


# ---------------------------------------------------------------- per-call model (routing + escalation)

def on_llm_request(request=None, session_id: str = "", model: str = "", **_):
    if not isinstance(request, dict) or not session_id or not _decision.enabled:
        return None
    s = _sessions.get(session_id)
    try:
        out = _decision.post("/v1/models/resolve", {"session_id": session_id,
                                                    "requested_model": request.get("model") or model},
                             timeout=float(os.environ.get("AIOS_RESOLVE_TIMEOUT", "2")))
    except client.ServiceError as exc:
        log.warning("model resolve failed, keeping %s: %s", request.get("model"), exc)
        return None
    resolved = out.get("model")
    if not resolved:
        return None
    s.model = resolved
    if resolved == request.get("model"):
        return None
    updated = dict(request)
    updated["model"] = resolved
    return {"request": updated, "source": "aios", "reason": f"decision: {out.get('source')} -> {resolved}"}


# ---------------------------------------------------------------- usage / budget

def on_post_api_request(session_id: str = "", api_request_id=None, usage=None, model: str = "",
                        api_duration=None, **_):
    if not session_id or not _decision.enabled or not isinstance(usage, dict):
        return
    s = _sessions.get(session_id)
    body = {
        "session_id": session_id, "agent": permissions.profile_name(), "model": s.model or model,
        "input_tokens": int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "cache_read_tokens": int(usage.get("cache_read_tokens") or 0),
        "api_request_id": str(api_request_id or ""), "purpose": "execute",
        "latency_ms": int(float(api_duration or 0) * 1000), "skills": sorted(s.skills),
    }
    if task := os.environ.get("HERMES_KANBAN_TASK"):
        body["task_id"] = task

    def send():
        try:
            out = _decision.post("/v1/usage", body)
            s.budget_state = (out.get("budget") or {}).get("state", s.budget_state)
        except client.ServiceError as exc:
            log.warning("usage not recorded: %s", exc)

    checks.background(send)


def on_api_request_error(session_id: str = "", status_code=None, reason=None, **_):
    if session_id:
        log.info("provider error session=%s status=%s reason=%s", session_id, status_code, reason)


# ---------------------------------------------------------------- tool gate

def on_pre_tool_call(tool_name: str = "", args=None, session_id: str = "", **_):
    args = args if isinstance(args, dict) else {}
    s = _sessions.get(session_id) if session_id else None
    tenant = (s.tenant if s else None) or os.environ.get("HERMES_TENANT") or None
    verdict = permissions.check_tool(_agent(), tool_name, args, tenant)
    if not verdict.allow:
        log.warning("blocked tool %s for %s: %s", tool_name, permissions.profile_name(), verdict.reason)
        return {"action": "block", "message": "[aios] " + verdict.reason}
    if s and s.budget_state == "exhausted" and tool_name not in WRAP_UP_TOOLS:
        return {"action": "block", "message": "[aios] orçamento da tarefa esgotado: finalize com um relatório "
                                              "(kanban_complete/kanban_block) sem executar novas ferramentas."}
    return None


def on_post_tool_call(tool_name: str = "", args=None, result=None, status: str = "", session_id: str = "", **_):
    if not session_id:
        return
    s = _sessions.get(session_id)
    if tool_name == "terminal" and isinstance(args, dict) and checks.looks_like_test(args.get("command", "")):
        s.last_test = checks.parse_terminal_result(args.get("command", ""), result)
        checks.background(lambda: checks.emit_event("test.result", {"session_id": session_id, **s.last_test}))
    elif tool_name == "kanban_complete" and status == "ok":
        s.outcome = "succeeded"
        checks.background(lambda: _finish(session_id, "succeeded"))


# ---------------------------------------------------------------- test / repair / escalate gate (§7)

def on_pre_verify(session_id: str = "", coding: bool = False, attempt: int = 0, changed_paths=None, **_):
    if not coding or not session_id or not _decision.enabled:
        return None
    s = _sessions.get(session_id)
    if s.outcome in ("failed", "blocked") or attempt >= int(os.environ.get("AIOS_MAX_VERIFY", "8")):
        return None
    paths = [p for p in (changed_paths or []) if isinstance(p, str)]
    result = checks.run_check(paths[0] if paths else None)
    evidence = {"validation": "none"}
    if result is not None:
        evidence = {"validation": "pass" if result["exit_code"] == 0 else "fail",
                    "tests": {"exit_code": result["exit_code"], "output_tail": result.get("output_tail", "")[-6000:],
                              "command": result.get("command", "")}}
    elif s.last_test is not None:
        evidence = {"validation": "pass" if s.last_test["exit_code"] == 0 else "fail", "tests": s.last_test}
    try:
        gate = _decision.post("/v1/gate", {"session_id": session_id, "agent": permissions.profile_name(),
                                           "evidence": evidence}, timeout=30)
    except client.ServiceError as exc:
        log.warning("gate unavailable, letting the turn finish: %s", exc)
        return None

    action = gate.get("action")
    s.model = (gate.get("run") or {}).get("model") or s.model
    if action in ("repair", "escalate"):
        return {"action": "continue", "message": "[aios gate: " + action + "] " + gate.get("message", "")}
    if action in ("fail", "ask_human"):
        s.outcome = "failed" if action == "fail" else "blocked"
        return {"action": "continue", "message": "[aios gate: " + action + "] " + gate.get("message", "")}
    return None


# ---------------------------------------------------------------- lifecycle

def on_skill_lifecycle(skill_name: str = "", session_id: str = "", action: str = "", **_):
    if session_id and skill_name and action in ("view", "load", "use", "used", "loaded", "invoked"):
        _sessions.get(session_id).skills.add(skill_name)


def on_session_finalize(session_id: str = "", interrupted: bool = False, **_):
    if not session_id or not _decision.enabled:
        return
    s = _sessions.peek(session_id)
    if s is None or not s.routed:
        return
    status = {"failed": "failed", "blocked": "cancelled"}.get(s.outcome or "", "cancelled" if interrupted else "succeeded")
    checks.background(lambda: _finish(session_id, status))
    _sessions.drop(session_id)


def _finish(session_id: str, status: str) -> None:
    try:
        _decision.post(f"/v1/runs/{session_id}/finish", {"status": status})
    except client.ServiceError as exc:
        log.warning("finish not recorded: %s", exc)
