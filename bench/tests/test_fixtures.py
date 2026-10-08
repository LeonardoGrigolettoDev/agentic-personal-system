"""Coding fixtures are real repos with deterministic checks: the pristine fixture must FAIL its check and the
reference solution (tests/solutions/<key>, never staged for the agent) must PASS it, with the protected test
files untouched. Runs locally through the same LocalWorkspace + evaluate() path the runner uses."""

import shutil
import subprocess

import pytest
from conftest import FIXTURES_DIR, SOLUTIONS_DIR, TASKS_DIR

from bench.checks import evaluate, sha256_files
from bench.tasks import load_tasks
from bench.workspace import LocalWorkspace

COMMAND_TASKS = [t for t in load_tasks(TASKS_DIR) if t.check["type"] == "command"]


def needs(task) -> list[str]:
    text = task.check["run"] + (task.setup or "")
    tools = [tool for tool, marker in (("go", "go "), ("node", "node "), ("python3", "python3"), ("git", "git "))
             if marker in text]
    if (FIXTURES_DIR / task.fixture / "go.mod").exists() or task.key == "agentic-cli-gastos-go":
        tools.append("go")
    return sorted(set(tools))


def run_check(task, tmp_path, solution: bool):
    ws = LocalWorkspace(tmp_path / ("solved" if solution else "pristine"))
    workdir = ws.stage(FIXTURES_DIR / task.fixture, f"bench-{task.key}")
    if task.setup:
        assert ws.run(workdir, task.setup, 120).exit_code == 0
    if solution:
        shutil.copytree(SOLUTIONS_DIR / task.key, workdir, dirs_exist_ok=True)
        if (SOLUTIONS_DIR / task.key / "solve.sh").exists():
            assert ws.run(workdir, "bash solve.sh", 120).exit_code == 0
    protect = task.check.get("protect") or []
    hashes = sha256_files(FIXTURES_DIR / task.fixture, protect) if protect else None
    return evaluate(task.check, answer="", workspace=ws, workdir=workdir, protect_hashes=hashes)


def test_every_command_task_has_a_reference_solution():
    assert len(COMMAND_TASKS) >= 20
    missing = [t.key for t in COMMAND_TASKS if not (SOLUTIONS_DIR / t.key).is_dir()]
    assert missing == []


def test_solutions_never_live_in_fixtures():
    assert not any(p.name in ("solutions", "solve.sh") for p in FIXTURES_DIR.rglob("*"))


@pytest.mark.toolchain
@pytest.mark.parametrize("task", COMMAND_TASKS, ids=lambda t: t.key)
def test_pristine_fails_and_solution_passes(task, tmp_path):
    missing = [tool for tool in needs(task) if shutil.which(tool) is None]
    if missing:
        pytest.skip(f"needs {', '.join(missing)}")
    pristine = run_check(task, tmp_path, solution=False)
    assert pristine.error is None
    assert not pristine.passed, f"pristine fixture already passes: {pristine.detail.get('output_tail', '')[-500:]}"
    solved = run_check(task, tmp_path, solution=True)
    assert solved.error is None
    assert solved.passed, solved.detail.get("output_tail", "")[-1500:]


@pytest.mark.toolchain
def test_bisect_setup_builds_a_repo_whose_first_bad_commit_matches_the_check(tmp_path, task_by_key):
    if not (shutil.which("git") and shutil.which("python3")):
        pytest.skip("needs git + python3")
    task = task_by_key["agentic-git-bisect-frete"]
    ws = LocalWorkspace(tmp_path)
    workdir = ws.stage(FIXTURES_DIR / task.fixture, "bench-bisect")
    assert ws.run(workdir, task.setup, 120).exit_code == 0
    assert not (tmp_path / "bench-bisect" / "criar-repo.sh").exists()
    out = subprocess.run("git bisect start HEAD v1.0 >/dev/null && git bisect run python3 -m unittest -q 2>/dev/null;"
                         " git bisect reset -q >/dev/null 2>&1", shell=True, cwd=f"{workdir}/repo",
                         capture_output=True, text=True, timeout=120, check=False).stdout
    culprit = out.split("is the first bad commit", 1)[1]
    assert evaluate(task.check, answer=culprit).passed, culprit[:300]
