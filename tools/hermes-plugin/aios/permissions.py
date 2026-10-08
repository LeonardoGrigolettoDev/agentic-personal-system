"""Per-agent tool / tenant / domain policy (docs/ARCHITECTURE.md §12), evaluated locally with zero tokens.

agents/<profile>/agent.yaml lists tool *categories*; Hermes tool names map onto them below. Deny wins.
Hermes' own per-profile toolsets are the first allowlist; this is the second, policy-level gate.
"""

import fnmatch
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - Hermes ships PyYAML
    yaml = None

# Hermes tool name -> policy category. Tools not listed are core (memory, todo, clarify, skills...).
TOOL_CATEGORIES = {
    "terminal": "shell",
    "execute_code": "shell",
    "process_manage": "shell",
    "read_file": "filesystem",
    "write_file": "filesystem",
    "patch": "filesystem",
    "search_files": "filesystem",
    "web_search": "web_research",
    "web_extract": "web_research",
    "browser_*": "web_research",
    "delegate_task": "delegate",
    "kanban_*": "tasks",
    "cronjob_manage": "tasks",
    "send_message": "messaging",
    "mcp__knowledge__*": "knowledge",
    "mcp__*": "mcp",
}

SECRET_PATH = re.compile(r"(^|/)(\.env(\.(?!example$|sample$|template$)[\w-]+)?|[^/]*\.pem|id_(rsa|ed25519|ecdsa)(\.pub)?|"
                         r"secrets?\.(ya?ml|json|toml|env|txt)|\.ssh/[^/]+|\.netrc|\.git-credentials|credentials\.json)$",
                         re.IGNORECASE)
SECRET_IN_COMMAND = re.compile(
    r"(^|[\s'\"=/<])(\.env(?!\.(example|sample|template)\b)\b|[\w./-]*\.pem\b|id_(rsa|ed25519|ecdsa)\b|\.ssh/|"
    r"\.git-credentials|\.netrc)",
    re.IGNORECASE)
DESTRUCTIVE = [
    (re.compile(r"\brm\s+(-[a-z]*r[a-z]*f|-[a-z]*f[a-z]*r)[a-z]*\s+(/|~|\$HOME|\*)(\s|$)", re.I), "rm -rf on a root/home path"),
    (re.compile(r"\bgit\s+push\b[^\n]*(--force\b|-f\b)[^\n]*\b(main|master)\b", re.I), "force push to main/master"),
    (re.compile(r"\b(drop\s+(database|schema)|truncate\s+table)\b", re.I), "destructive SQL"),
    (re.compile(r"\bmkfs(\.\w+)?\b|\bdd\s+[^\n]*of=/dev/", re.I), "disk formatting"),
    (re.compile(r":\(\)\s*\{\s*:\|:&\s*\};:"), "fork bomb"),
    (re.compile(r"\bcurl\b[^\n|]*\|\s*(sudo\s+)?(ba)?sh\b", re.I), "piping remote script into a shell"),
]


@dataclass
class AgentPolicy:
    slug: str
    allowed_tools: set[str] = field(default_factory=set)
    allowed_tenants: set[str] = field(default_factory=set)
    allowed_domains: set[str] = field(default_factory=set)
    deny_domains: set[str] = field(default_factory=set)
    max_tier: int = 5


@dataclass(frozen=True)
class Verdict:
    allow: bool
    reason: str = ""


def category(tool_name: str) -> str | None:
    for pattern, cat in TOOL_CATEGORIES.items():
        if fnmatch.fnmatchcase(tool_name, pattern):
            return cat
    return None


def load_agents(directory: Path) -> dict[str, AgentPolicy]:
    agents: dict[str, AgentPolicy] = {}
    if yaml is None or not directory.is_dir():
        return agents
    for path in sorted(directory.glob("*/agent.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        slug = data.get("agent") or path.parent.name
        agents[slug] = AgentPolicy(
            slug=slug,
            allowed_tools=set(data.get("allowed_tools") or []),
            allowed_tenants=set(data.get("allowed_tenants") or []),
            allowed_domains=set(data.get("allowed_domains") or []),
            deny_domains=set(data.get("deny_domains") or []),
            max_tier=int(data.get("max_tier") or 5),
        )
    return agents


def _paths(args: dict) -> list[str]:
    return [v for k in ("path", "file_path", "target", "source", "destination") if isinstance(v := args.get(k), str)]


def check_tool(agent: AgentPolicy | None, tool_name: str, args: dict, tenant: str | None) -> Verdict:
    """Deny > allow. Secret files and destructive commands are blocked for every agent."""
    for p in _paths(args):
        if SECRET_PATH.search(p.replace("\\", "/")):
            return Verdict(False, f"acesso a arquivo sensível bloqueado: {p}")
    command = args.get("command") or args.get("code") or ""
    if isinstance(command, str) and command:
        if SECRET_IN_COMMAND.search(command):
            return Verdict(False, "comando referencia arquivo sensível (.env, chaves, credenciais)")
        for pattern, label in DESTRUCTIVE:
            if pattern.search(command):
                return Verdict(False, f"comando destrutivo bloqueado ({label}); peça aprovação humana explícita")

    if agent is None:
        return Verdict(True)
    cat = category(tool_name)
    if cat is not None and cat not in agent.allowed_tools and "*" not in agent.allowed_tools:
        return Verdict(False, f"o agente '{agent.slug}' não tem permissão para ferramentas de '{cat}' ({tool_name})")

    arg_tenant = args.get("tenant")
    if isinstance(arg_tenant, str) and arg_tenant:
        # 'shared' holds global preferences/rules: readable by every agent
        if arg_tenant != "shared" and agent.allowed_tenants and arg_tenant not in agent.allowed_tenants:
            return Verdict(False, f"o agente '{agent.slug}' não acessa o tenant '{arg_tenant}'")
        if tenant and arg_tenant not in (tenant, "shared"):
            return Verdict(False, f"esta sessão é do tenant '{tenant}'; acesso a '{arg_tenant}' bloqueado (isolamento)")
    arg_domain = args.get("domain")
    if isinstance(arg_domain, str) and arg_domain in agent.deny_domains:
        return Verdict(False, f"o agente '{agent.slug}' não acessa o domínio '{arg_domain}'")
    return Verdict(True)


def profile_name() -> str:
    """Hermes profile the current turn runs FOR. A multiplexed gateway serves /p/<profile>/ requests and cron
    ticks from one process whose environment is the launch (chief) profile's, so ask Hermes first; the
    Kanban dispatcher also pins HERMES_PROFILE on its workers. The default home is the chief."""
    name = os.environ.get("AIOS_AGENT") or ""
    if not name:
        try:
            from hermes_cli.profiles import current_profile_name
            name = current_profile_name() or ""
        except Exception:  # noqa: BLE001 - outside Hermes (tests) or an API change: fall back to the env pin
            name = ""
    name = name or os.environ.get("HERMES_PROFILE") or "chief"
    return "chief" if name in ("default", "custom", "") else name
