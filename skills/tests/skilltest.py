"""Shared paths and helpers for the skills test-suite (stdlib + pytest + pyyaml only)."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import yaml

SKILLS_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SKILLS_ROOT.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"

# ARCHITECTURE §5 / CONTRACTS §3: the skills this repository must ship.
EXPECTED_SKILLS = {
    "coding": {"repository_analysis", "debugging", "code_review", "architecture_review", "test_generation"},
    "finance": {"spreadsheet_analysis", "budget_review", "expense_categorization"},
    "research": {"web_research", "source_synthesis"},
    "projects": {"project_status", "roadmap_update", "decision_record"},
    "productivity": {"daily_planning", "daily_review", "weekly_review"},
    "knowledge": {"knowledge_capture", "memory_hygiene", "transcript_to_notes"},
}
EXPECTED_WORKFLOWS = {"morning-review", "daily-planning", "engineering-review", "project-review",
                      "learning-review", "daily-reflection", "weekly-review"}


def workflow_files() -> list[Path]:
    return sorted((REPO_ROOT / "workflows").glob("*.md"))


def skill_files() -> list[Path]:
    return sorted(p for p in SKILLS_ROOT.rglob("SKILL.md") if "tests" not in p.relative_to(SKILLS_ROOT).parts)


def split_frontmatter(text: str) -> tuple[dict, str]:
    """Same fence rules as Hermes (agent.skill_utils.parse_frontmatter), strict YAML."""
    text = text.removeprefix("﻿")
    assert text.startswith("---"), "SKILL.md must start with YAML frontmatter (---)"
    end = re.search(r"\n---\s*\n", text[3:])
    assert end, "frontmatter is not closed with '---'"
    data = yaml.safe_load(text[3:end.start() + 3])
    assert isinstance(data, dict), "frontmatter must be a YAML mapping"
    return data, text[end.end() + 3:]


def script(rel: str) -> Path:
    path = SKILLS_ROOT / rel
    assert path.is_file(), f"missing script {rel}"
    return path


def run(cmd: list[str], *, env: dict | None = None, cwd: Path | None = None, stdin: str | None = None,
        timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=cwd, input=stdin, timeout=timeout)


def run_json(cmd: list[str], **kw) -> dict:
    proc = run(cmd, **kw)
    assert proc.returncode == 0, f"exit {proc.returncode}\nstdout: {proc.stdout[-2000:]}\nstderr: {proc.stderr[-2000:]}"
    return json.loads(proc.stdout)

