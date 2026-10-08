"""Validate the skills tree with Hermes' own code (run with the Hermes interpreter).

    <hermes-python> skills/tests/hermes_validate.py [SKILLS_ROOT] [--workflows DIR]

Uses a throwaway HERMES_HOME whose config.yaml points ``skills.external_dirs`` at SKILLS_ROOT, then:
  - hard frontmatter validation for new skills (skill_manager_tool._validate_frontmatter)
  - the authoring linter (tools.skill_linter.lint_skill) — every finding counts as a failure
  - the install-time security scanner (tools.skills_guard.scan_skill) — verdict must be "safe"
  - discovery: skills_list() lists every skill under its category, skill_view() loads it by bare and
    by categorized name without security warnings, and the system-prompt skill index includes it
  - the cron prompt scanners on workflows/*.md (strict) and on each SKILL.md body (assembled)
Prints a JSON report; exit 1 on any failure. Never touches the real ~/.hermes.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--workflows", default=None, help="workflows dir (default: <root>/../workflows)")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    workflows = Path(args.workflows).resolve() if args.workflows else root.parent / "workflows"

    home = Path(tempfile.mkdtemp(prefix="hermes-skills-validate-"))
    (home / "config.yaml").write_text(f"skills:\n  external_dirs: [{json.dumps(str(root))}]\n", encoding="utf-8")
    os.environ["HERMES_HOME"] = str(home)

    warnings: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.levelno >= logging.WARNING:
                warnings.append(f"{record.name}: {record.getMessage()}")

    logging.getLogger().addHandler(Capture())
    logging.getLogger().setLevel(logging.WARNING)

    from agent.prompt_builder import build_skills_system_prompt
    from tools.cronjob_prompt_scan import _scan_cron_prompt, _scan_cron_skill_assembled
    from tools.skill_linter import lint_skill
    from tools.skill_manager_tool import _validate_frontmatter
    from tools.skills_guard import scan_skill
    from tools.skills_tool import skill_view, skills_list

    failures: list[str] = []
    skill_files = sorted(p for p in root.rglob("SKILL.md") if "tests" not in p.relative_to(root).parts)
    listed = {s["name"]: s for s in json.loads(skills_list()).get("skills", [])}
    index = build_skills_system_prompt()
    report = {"hermes_home": str(home), "root": str(root), "skills": [], "workflows": []}

    for skill_md in skill_files:
        rel = skill_md.parent.relative_to(root)
        name, category = skill_md.parent.name, rel.parts[0]
        content = skill_md.read_text(encoding="utf-8")
        entry = {"skill": str(rel), "problems": []}
        if err := _validate_frontmatter(content, new_skill=True):
            entry["problems"].append(f"frontmatter: {err}")
        entry["problems"] += [f"lint {f.severity} {f.rule}: {f.message}" for f in lint_skill(skill_md)]
        scan = scan_skill(skill_md.parent, source="local")
        if scan.verdict != "safe":
            entry["problems"].append(f"guard verdict {scan.verdict}: " +
                                     "; ".join(f"{f.pattern_id}@{f.file}:{f.line}" for f in scan.findings))
        if listed.get(name, {}).get("category") != category:
            entry["problems"].append(f"skills_list: missing or wrong category ({listed.get(name)})")
        for lookup in (name, f"{category}/{name}"):
            before = len(warnings)
            view = json.loads(skill_view(lookup))
            if not view.get("success"):
                entry["problems"].append(f"skill_view({lookup}): {view.get('error')}")
            new_warnings = warnings[before:]
            if new_warnings:
                entry["problems"].append(f"skill_view({lookup}) warnings: {new_warnings}")
        if f"- {name}:" not in index:
            entry["problems"].append("not in the system-prompt skill index")
        _, assembled_err = _scan_cron_skill_assembled(content)
        if assembled_err:
            entry["problems"].append(f"cron assembled scan: {assembled_err}")
        failures += [f"{rel}: {p}" for p in entry["problems"]]
        report["skills"].append(entry)

    for wf in sorted(workflows.glob("*.md")):
        err = _scan_cron_prompt(wf.read_text(encoding="utf-8").strip())
        report["workflows"].append({"workflow": wf.name, "problem": err or None})
        if err:
            failures.append(f"workflows/{wf.name}: {err}")

    report["count"] = len(skill_files)
    report["failures"] = failures
    json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 1 if failures or not skill_files else 0


if __name__ == "__main__":
    sys.exit(main())
