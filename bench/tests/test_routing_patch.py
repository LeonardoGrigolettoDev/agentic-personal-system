from pathlib import Path

import pytest
import yaml
from conftest import ROUTING_FILE

from bench.cli import build_report
from bench.report import Suggestion
from bench.routing_patch import Change, PatchError, apply_changes, plan_changes, unified_diff

# frozen copy of config/routing.yaml (2026-10-07) so exact expectations don't drift with policy edits
REAL = (Path(__file__).parent / "data" / "routing.sample.yaml").read_text(encoding="utf-8")


def test_patching_the_live_routing_yaml_round_trips():
    live = ROUTING_FILE.read_text(encoding="utf-8")
    doc = yaml.safe_load(live)
    changes = []
    for task_type, spec in doc["task_types"].items():
        level, current = next(iter(spec["tier_by_complexity"].items()))
        other = next(m for m in doc["models"] if m != current)
        changes.append(Change(task_type, level, current, other))
    new_doc = yaml.safe_load(apply_changes(live, changes))
    for c in changes:
        doc["task_types"][c.task_type]["tier_by_complexity"][c.complexity] = c.new
    assert new_doc == doc


def test_plan_changes_only_touches_benchmarked_levels_that_differ():
    routing = yaml.safe_load(REAL)
    sugg = [Suggestion("summary", "tier2-cheap", 0.9, 0.001, 10, ("medium", "simple")),
            Suggestion("debugging", "tier3-code", 0.9, 0.2, 10, ("medium",)),  # already tier3-code
            Suggestion("nao_existe", "tier3-code", 0.9, 0.2, 10, ("medium",))]
    assert plan_changes(sugg, routing) == [Change("summary", "medium", "tier2-flash", "tier2-cheap")]


def test_apply_to_the_real_routing_yaml_keeps_everything_else():
    changes = [Change("summary", "medium", "tier2-flash", "tier2-cheap"),
               Change("debugging", "hard", "tier4-pro", "tier3-code"),
               Change("debugging", "critical", "tier5-sonnet", "tier4-pro")]
    new = apply_changes(REAL, changes)
    old_doc, new_doc = yaml.safe_load(REAL), yaml.safe_load(new)
    assert new_doc["task_types"]["summary"]["tier_by_complexity"]["medium"] == "tier2-cheap"
    assert new_doc["task_types"]["debugging"]["tier_by_complexity"] == {
        "trivial": "tier3-code", "simple": "tier3-code", "medium": "tier3-code", "hard": "tier3-code",
        "critical": "tier4-pro"}
    old_doc["task_types"]["summary"]["tier_by_complexity"]["medium"] = "tier2-cheap"
    old_doc["task_types"]["debugging"]["tier_by_complexity"].update(hard="tier3-code", critical="tier4-pro")
    assert new_doc == old_doc
    diff = unified_diff(REAL, new)
    assert diff.startswith("--- a/config/routing.yaml\n+++ b/config/routing.yaml\n")
    assert sum(1 for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++")) == 2
    assert "+    tier_by_complexity: { trivial: tier3-code, simple: tier3-code, medium: tier3-code, " \
           "hard: tier3-code, critical: tier4-pro }" in diff
    # comments survive
    assert new.count("#") == REAL.count("#")


BLOCK = """\
# comment kept
task_types:
  summary:
    description: resumir
    tier_by_complexity:
      simple: tier2-cheap   # cheap first
      medium: tier2-flash
    skills: [x]
  research:
    tier_by_complexity: { simple: tier2-flash }
models: {}
"""


def test_block_style_mapping_and_missing_level_are_supported():
    new = apply_changes(BLOCK, [Change("summary", "medium", "tier2-flash", "tier3-code"),
                                Change("summary", "hard", None, "tier4-pro")])
    doc = yaml.safe_load(new)
    assert doc["task_types"]["summary"]["tier_by_complexity"] == {
        "simple": "tier2-cheap", "medium": "tier3-code", "hard": "tier4-pro"}
    assert "simple: tier2-cheap   # cheap first" in new
    assert doc["task_types"]["summary"]["skills"] == ["x"]


def test_unknown_task_type_raises():
    with pytest.raises(PatchError):
        apply_changes(BLOCK, [Change("planning", "simple", None, "tier2-cheap")])
    with pytest.raises(PatchError):
        apply_changes("models: {}\n", [Change("summary", "simple", None, "tier2-cheap")])


def test_no_changes_no_diff():
    assert apply_changes(REAL, []) == REAL
    assert unified_diff(REAL, REAL) == ""


def rows(task_type, model, n, ok, cost, complexity="medium"):
    return [{"category": "simple", "start_model": model, "success": i < ok, "cost_usd": cost, "input_tokens": 100,
             "output_tokens": 50, "iterations": 1, "escalations": 0, "router_task_type_ok": True,
             "router_domain_ok": None, "error": None, "expected_task_type": task_type, "task_type": task_type,
             "complexity": complexity} for i in range(n)]


def test_build_report_end_to_end_prints_tables_and_a_patch():
    data_rows = rows("summary", "tier2-flash", 5, 4, 0.02) + rows("summary", "tier2-cheap", 5, 5, 0.004)
    text, data = build_report(data_rows, REAL, min_runs=3)
    assert "## category x start model" in text and "## task type x start model" in text
    assert "summary: tier2-cheap (success 100%" in text
    assert data["changes"] == [{"task_type": "summary", "complexity": "medium", "old": "tier2-flash",
                                "new": "tier2-cheap"}]
    assert "-    tier_by_complexity: { trivial: tier2-cheap, simple: tier2-cheap, medium: tier2-flash" in text
    assert "NOT applied" in text
    assert data["target_success_rate"] == 0.8  # read from routing.yaml learning.target_success_rate


def test_build_report_without_qualifying_data():
    text, data = build_report(rows("summary", "tier2-cheap", 5, 2, 0.001), REAL)
    assert "no (task type, model) pair qualifies yet" in text and data["routing_patch"] == ""
