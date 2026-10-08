"""Every regex / json_schema task is satisfiable by a realistic answer and rejects a plausible wrong one."""

import pytest
from conftest import TASKS_DIR

from bench.checks import evaluate
from bench.tasks import load_tasks

ANSWER_CHECKED = [t for t in load_tasks(TASKS_DIR) if t.check["type"] in ("regex", "json_schema")]


def test_every_answer_checked_task_has_golden_answers(golden):
    assert sorted(golden) == sorted(t.key for t in ANSWER_CHECKED)


@pytest.mark.parametrize("task", ANSWER_CHECKED, ids=lambda t: t.key)
def test_golden_answers(task, golden):
    samples = golden[task.key]
    assert samples["pass"] and samples["fail"]
    for answer in samples["pass"]:
        res = evaluate(task.check, answer=answer)
        assert res.passed, f"should pass: {answer!r} -> {res.detail}"
    for answer in samples["fail"]:
        res = evaluate(task.check, answer=answer)
        assert not res.passed, f"should fail: {answer!r} -> {res.detail}"
