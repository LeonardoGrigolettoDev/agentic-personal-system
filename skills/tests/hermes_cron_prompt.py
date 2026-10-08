"""Where the tenant tag lands in each cron job's first message, assembled by Hermes itself.

    <hermes-python> skills/tests/hermes_cron_prompt.py SKILLS_ROOT WORKFLOWS_DIR JOBS_JSON

JOBS_JSON maps workflow name -> list of attached skills (from config/hermes/profiles.yaml). For every
workflows/*.md it runs cron.scheduler._build_job_prompt twice — as configured, and with no attached
skills — and prints {name: {"configured": offset, "bare": offset, "length": chars}} where offset is the
character index of "Tenant desta execução" (-1 if absent). Uses a throwaway HERMES_HOME.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

TAG = "Tenant desta execução"


def main() -> int:
    root, workflows, jobs = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve(), json.loads(sys.argv[3])
    home = Path(tempfile.mkdtemp(prefix="hermes-cron-prompt-"))
    (home / "config.yaml").write_text(f"skills:\n  external_dirs: [{json.dumps(str(root))}]\n", encoding="utf-8")
    os.environ["HERMES_HOME"] = str(home)

    from cron.scheduler import _build_job_prompt

    report = {}
    for path in sorted(workflows.glob("*.md")):
        prompt = path.read_text(encoding="utf-8").strip()
        configured = _build_job_prompt({"id": path.stem, "name": path.stem, "prompt": prompt,
                                        "skills": jobs.get(path.stem) or []}) or ""
        bare = _build_job_prompt({"id": path.stem, "name": path.stem, "prompt": prompt, "skills": []}) or ""
        report[path.stem] = {"configured": configured.find(TAG), "bare": bare.find(TAG), "length": len(configured)}
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
