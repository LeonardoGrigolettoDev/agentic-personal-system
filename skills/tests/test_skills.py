"""Structure tests: every SKILL.md follows Hermes' skill format and authoring rules (v2026.9.24:
tools/skill_manager_tool._validate_frontmatter, tools/skill_linter, agent/skill_utils), referenced
files exist, workflows are valid cron prompts, and repo config points at real skills."""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import pytest
import yaml
from skilltest import (
    EXPECTED_SKILLS,
    EXPECTED_WORKFLOWS,
    REPO_ROOT,
    SKILLS_ROOT,
    run,
    skill_files,
    split_frontmatter,
    workflow_files,
)

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")       # skill_linter name-format
DESC_PROMPT_LIMIT = 60                                 # agent.skill_utils.SKILL_PROMPT_DESC_LIMIT
BODY_SOFT_BUDGET = 24_000                              # skill_linter oversized-body
MARKETING = ("powerful", "comprehensive", "seamless", "advanced", "cutting-edge", "state-of-the-art",
             "revolutionary", "robust")
SHELL_UTILS = ("grep", "rg", "cat", "head", "tail", "sed", "awk", "find", "ls")
INJECTION = ("ignore previous instructions", "ignore all previous", "you are now", "disregard your",
             "forget your instructions", "new instructions:", "system prompt:", "<system>", "]]>")
TOP_KEYS = {"name", "description", "version", "author", "license", "platforms", "metadata",
            "required_environment_variables", "required_credential_files", "environments"}
HERMES_META_KEYS = {"tags", "category", "related_skills", "requires_toolsets", "requires_tools",
                    "fallback_for_toolsets", "fallback_for_tools", "config", "blueprint", "homepage"}
FORBIDDEN_IN_SKILL_DIR = ("README.md", "CHANGELOG.md", "install.sh", ".env", ".env.example", ".gitignore")
# Cron create-time scanner (tools/cronjob_prompt_scan.py): user prompts must not match these.
CRON_STRICT = [
    r"ignore\s+(?:\w+\s+)*(?:previous|all|above|prior)\s+(?:\w+\s+)*instructions",
    r"do\s+not\s+tell\s+the\s+user", r"system\s+prompt\s+override",
    r"disregard\s+(your|all|any)\s+(instructions|rules|guidelines)",
    r"cat\s+[^\n]*(\.env|credentials|\.netrc|\.pgpass|id_rsa|id_ed25519|id_ecdsa)",
    r"authorized_keys", r"/etc/sudoers|visudo", r"rm\s+-rf\s+/",
    r"curl\s+[^\n]*\$\{?\w*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)\w*\}?",
]

SKILLS = skill_files()
ALL_NAMES = {p.parent.name for p in SKILLS}
WORKFLOWS = workflow_files()
KB_INGEST = REPO_ROOT / "workers" / "kb" / "src" / "kb" / "ingest.py"
CRON_HINT_CHARS = 1200      # Hermes cron/scheduler_prompt._CRON_HINT is 1127 chars (v2026.9.24), prepended
ROUTE_WINDOW_CHARS = 4000   # aios plugin sends text[:4000] of the first message to decision /v1/route


def _id(path: Path) -> str:
    return str(path.parent.relative_to(SKILLS_ROOT))


def _prose(body: str) -> str:
    return re.sub(r"```.*?```", "", body, flags=re.S)


def test_expected_skill_set_is_present():
    found = {}
    for path in SKILLS:
        category, name = path.parent.relative_to(SKILLS_ROOT).parts
        found.setdefault(category, set()).add(name)
    assert found == EXPECTED_SKILLS


def test_skill_names_are_unique():
    assert len(ALL_NAMES) == len(SKILLS)


@pytest.mark.parametrize("path", SKILLS, ids=_id)
def test_frontmatter_matches_hermes_rules(path: Path):
    fm, body = split_frontmatter(path.read_text(encoding="utf-8"))
    assert set(fm) <= TOP_KEYS, f"unsupported top-level keys: {set(fm) - TOP_KEYS}"
    name = fm["name"]
    assert name == path.parent.name, "frontmatter name must equal the directory name"
    assert NAME_RE.match(name) and len(name) <= 64
    desc = str(fm["description"]).strip().strip("'\"")
    assert 0 < len(desc) <= DESC_PROMPT_LIMIT, f"description has {len(desc)} chars (limit {DESC_PROMPT_LIMIT})"
    assert desc.endswith("."), "description must be one sentence ending with a period"
    assert not any(re.search(rf"\b{w}\b", desc.lower()) for w in MARKETING)
    for key in ("version", "author", "license"):
        assert fm.get(key), f"missing {key}"
    assert re.fullmatch(r"\d+\.\d+\.\d+", str(fm["version"]))
    assert set(fm.get("platforms") or []) <= {"linux", "macos", "windows"}
    hermes = fm["metadata"]["hermes"]
    assert set(hermes) <= HERMES_META_KEYS, f"unsupported metadata.hermes keys: {set(hermes) - HERMES_META_KEYS}"
    assert hermes["category"] == path.parent.parent.name
    assert isinstance(hermes["tags"], list) and hermes["tags"]
    assert set(hermes["related_skills"]) <= ALL_NAMES, "related_skills must name skills that exist"
    for env in fm.get("required_environment_variables") or []:
        assert re.fullmatch(r"[A-Z][A-Z0-9_]*", env["name"])
    assert body.strip(), "SKILL.md needs a body"


@pytest.mark.parametrize("path", SKILLS, ids=_id)
def test_body_follows_authoring_rules(path: Path):
    _, body = split_frontmatter(path.read_text(encoding="utf-8"))
    assert re.search(r"^#+\s+When to Use", body, re.M), "missing '## When to Use'"
    for section in ("Procedure", "Pitfalls", "Verification"):
        assert re.search(rf"^##\s+{section}\b", body, re.M), f"missing '## {section}'"
    assert len(body) <= BODY_SOFT_BUDGET
    prose = _prose(body)
    for util in SHELL_UTILS:
        assert f"`{util}`" not in prose, f"prose names shell utility `{util}`; name the Hermes tool instead"
    lowered = body.lower()
    assert not [p for p in INJECTION if p in lowered]
    assert "tenant" in body, "every skill must say which knowledge tenant to use"
    gate = re.search(r"^##\s+Gate\b.*?(?=^##\s)", body, re.M | re.S)
    assert gate, "missing '## Gate e escalonamento' (rely on the aios gate, never switch models)"
    section = gate.group(0).lower()
    assert "modelo" in section and "esgotad" in section, \
        "gate section must forbid asking for another model and say what to do when the budget is exhausted"


def _allowed_uri_schemes() -> tuple[str, ...]:
    """kb.ingest.ALLOWED_URI_SCHEMES, read from source (the kb package needs psycopg & co. to import)."""
    if not KB_INGEST.is_file():
        pytest.skip("workers/kb not present")
    match = re.search(r"^ALLOWED_URI_SCHEMES\s*=\s*(\(.*?\))\s*$", KB_INGEST.read_text(encoding="utf-8"), re.M)
    assert match, "ALLOWED_URI_SCHEMES not found in workers/kb/src/kb/ingest.py"
    return tuple(ast.literal_eval(match.group(1)))


def test_ingest_source_uris_use_schemes_the_knowledge_service_accepts():
    allowed = _allowed_uri_schemes()
    texts = [p for p in SKILLS_ROOT.rglob("*.md") if "tests" not in p.relative_to(SKILLS_ROOT).parts] + WORKFLOWS
    found = 0
    for path in texts:
        for uri in re.findall(r'source_uri="([^"]+)"', path.read_text(encoding="utf-8")):
            found += 1
            assert uri.startswith(allowed) and not uri.startswith("file:///"), \
                f"{path.relative_to(REPO_ROOT)}: source_uri {uri!r} is rejected by kb (allowed: {allowed})"
    assert found >= 5


@pytest.mark.parametrize("path", SKILLS, ids=_id)
def test_project_scoped_memory_documents_the_unknown_project_fallback(path: Path):
    """No tool registers knowledge projects yet: scope="project" fails with "unknown project"."""
    _, body = split_frontmatter(path.read_text(encoding="utf-8"))
    if 'scope="project"' in body:
        assert "unknown project" in body and "[projeto <slug>]" in body, \
            'memory_save(scope="project") needs the scope="domain" + "[projeto <slug>]" fallback'
    for call in re.findall(r"mcp__knowledge__memory_search\((.*?)\)`", body, re.S):
        assert "project=" not in call, "memory_search(project=...) hides memories saved by the fallback"


@pytest.mark.parametrize("path", SKILLS, ids=_id)
def test_referenced_files_exist(path: Path):
    skill_dir = path.parent
    fm, body = split_frontmatter(path.read_text(encoding="utf-8"))
    for rel in set(re.findall(r"(?:references|templates|assets)/[\w./-]+\w", body)):
        assert (skill_dir / rel).is_file(), f"body references missing {rel}"
    related_dirs = [p.parent for p in SKILLS if p.parent.name in fm["metadata"]["hermes"]["related_skills"]]
    for rel in set(re.findall(r"scripts/[\w.-]+\.(?:py|sh)", body)):
        owners = [d for d in [skill_dir, *related_dirs] if (d / rel).is_file()]
        assert owners, f"body references missing {rel}"
    for category, name in re.findall(r"external_skills/\*/([a-z_]+)/([a-z_]+)", body):
        assert (category, name) == (skill_dir.parent.name, skill_dir.name), "sandbox fallback path points elsewhere"


@pytest.mark.parametrize("path", SKILLS, ids=_id)
def test_skill_dir_has_only_supported_layout(path: Path):
    skill_dir = path.parent
    for item in skill_dir.iterdir():
        assert item.name in ("SKILL.md", "scripts", "references", "templates", "assets"), f"unexpected {item.name}"
    for name in FORBIDDEN_IN_SKILL_DIR:
        assert not (skill_dir / name).exists()


def test_scripts_are_executable_with_shebang():
    scripts = [p for p in SKILLS_ROOT.glob("*/*/scripts/*") if p.suffix in (".py", ".sh")]
    assert scripts
    for path in scripts:
        assert os.access(path, os.X_OK), f"{path} is not executable"
        assert path.read_text(encoding="utf-8").startswith("#!/usr/bin/env "), f"{path} lacks a portable shebang"


def test_no_markdown_shadows_a_skill_name():
    """Hermes resolves a bare skill name also through any '<name>.md' under the skills dirs."""
    for md in SKILLS_ROOT.rglob("*.md"):
        assert md.stem not in ALL_NAMES, f"{md} would collide with skill '{md.stem}'"


def test_category_descriptions_exist():
    for category in EXPECTED_SKILLS:
        fm, _ = split_frontmatter((SKILLS_ROOT / category / "DESCRIPTION.md").read_text(encoding="utf-8"))
        assert fm.get("description")


def test_routing_policy_references_existing_skills():
    routing = yaml.safe_load((REPO_ROOT / "config" / "routing.yaml").read_text(encoding="utf-8"))
    for task, spec in routing["task_types"].items():
        for ref in spec.get("skills") or []:
            assert (SKILLS_ROOT / ref / "SKILL.md").is_file(), f"routing.yaml task {task} -> missing skill {ref}"


def _cron_jobs() -> dict[str, dict]:
    profiles = yaml.safe_load((REPO_ROOT / "config" / "hermes" / "profiles.yaml").read_text(encoding="utf-8"))
    return {job["name"]: job for job in profiles["cron"]}


def test_cron_jobs_have_workflows_and_skills():
    jobs = _cron_jobs()
    assert EXPECTED_WORKFLOWS <= set(jobs), f"required cron jobs missing: {EXPECTED_WORKFLOWS - set(jobs)}"
    for name, job in jobs.items():
        assert (REPO_ROOT / "workflows" / f"{name}.md").is_file()
        for ref in job.get("skills") or []:
            assert (SKILLS_ROOT / ref / "SKILL.md").is_file(), f"cron {name} -> missing skill {ref}"


def test_every_workflow_is_registered_or_documented():
    """A workflows/*.md that no cron job uses must be listed in skills/README.md as pending registration."""
    readme = (SKILLS_ROOT / "README.md").read_text(encoding="utf-8")
    for path in WORKFLOWS:
        assert path.stem in _cron_jobs() or f"`{path.stem}`" in readme, f"{path.name} is neither scheduled nor documented"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.stem)
def test_workflow_prompt_is_self_contained_and_safe(path: Path):
    text = path.read_text(encoding="utf-8").strip()
    assert not text.startswith("---"), "the whole file is the cron prompt: no frontmatter"
    tenants = set(re.findall(r'tenant="(nitro|pessoal|shared)"', text))
    declared = re.search(r"Tenant desta execução: \*\*(nitro|pessoal)\*\*", text.splitlines()[0])
    assert declared, "workflow must declare its tenant on the first line"
    assert tenants <= {declared.group(1), "shared"}, f"workflow mixes tenants: {tenants}"
    assert declared.end() + CRON_HINT_CHARS < ROUTE_WINDOW_CHARS, "tenant tag must be in the routed window"
    for needle in ("mcp__knowledge__", "memory_save", "kanban_create", "idempotency_key", "skill_view",
                   "orçamento esgotado", "paralelo"):
        assert needle in text, f"workflow must mention {needle}"
    for pattern in CRON_STRICT:
        assert not re.search(pattern, text, re.IGNORECASE), f"cron scanner would block: {pattern}"
    for skill in re.findall(r"skill `([a-z_]+)`", text):
        assert skill in ALL_NAMES
    for skill in re.findall(r'skill_view\(name="([a-z_]+)"\)', text):
        assert skill in ALL_NAMES
    for call in re.findall(r"mcp__knowledge__memory_search\((.*?)\)", text, re.S):
        assert "project=" not in call, "memory_search(project=...) hides memories saved by the fallback"


def test_chief_config_enables_kanban_toolset():
    """Hermes hides kanban_* from non-worker profiles unless they opt into the kanban toolset."""
    config = yaml.safe_load((REPO_ROOT / "config" / "hermes" / "config.yaml").read_text(encoding="utf-8"))
    platform = config.get("platform_toolsets") or {}
    lists = [config.get("toolsets") or []] + [v for v in platform.values() if isinstance(v, list)]
    assert any("kanban" in names for names in lists), "workflows need kanban_list/kanban_create in the chief"


@pytest.mark.skipif(not os.environ.get("HERMES_PYTHON"), reason="set HERMES_PYTHON to a Hermes venv python")
def test_hermes_itself_accepts_the_skills(tmp_path):
    proc = run([os.environ["HERMES_PYTHON"], str(SKILLS_ROOT / "tests" / "hermes_validate.py"), str(SKILLS_ROOT)],
               env={**os.environ, "TMPDIR": str(tmp_path)}, timeout=600)
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-4000:]


def _route_window() -> tuple[int, bool]:
    """(chars of the first message the aios plugin routes on, whether it reads the explicit tenant tag)."""
    plugin = REPO_ROOT / "tools" / "hermes-plugin" / "aios" / "__init__.py"
    source = plugin.read_text(encoding="utf-8") if plugin.is_file() else ""
    match = re.search(r'"text":\s*text\[:(\d+)\]', source)
    honours_tag = "Tenant desta execução" in source or "extract_user_instruction_from_skill_message" in source
    return (int(match.group(1)) if match else ROUTE_WINDOW_CHARS), honours_tag


@pytest.mark.skipif(not os.environ.get("HERMES_PYTHON"), reason="set HERMES_PYTHON to a Hermes venv python")
def test_cron_prompt_puts_tenant_tag_where_routing_sees_it(tmp_path):
    """Hermes prepends attached skill bodies to the cron prompt; the plugin routes on its first N chars."""
    import json

    jobs = {name: job.get("skills") or [] for name, job in _cron_jobs().items()}
    proc = run([os.environ["HERMES_PYTHON"], str(SKILLS_ROOT / "tests" / "hermes_cron_prompt.py"), str(SKILLS_ROOT),
                str(REPO_ROOT / "workflows"), json.dumps(jobs)], env={**os.environ, "TMPDIR": str(tmp_path)}, timeout=300)
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-4000:]
    report = json.loads(proc.stdout.strip().splitlines()[-1])
    window, honours_tag = _route_window()
    assert set(report) == {p.stem for p in WORKFLOWS}
    for name, pos in report.items():
        assert 0 <= pos["bare"] < window, f"{name}: tenant tag at {pos['bare']} even without attached skills"
    late = {name: pos["configured"] for name, pos in report.items() if not 0 <= pos["configured"] < window}
    if late and not honours_tag:
        pytest.xfail(f"owners config/hermes/profiles.yaml + aios plugin: with the attached skills the tenant tag "
                     f"lands past the {window}-char routing window {late}; drop cron `skills:` (workflows load "
                     f"them with skill_view) or route on the tag")
