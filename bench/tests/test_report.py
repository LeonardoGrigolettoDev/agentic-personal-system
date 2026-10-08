from datetime import UTC, datetime, timedelta

import pytest

from bench.report import Stats, by_category_model, by_task_type_model, parse_since, suggest, table


def row(category="debugging", model="tier3-code", success=True, cost=0.0, **kw):
    base = {"category": category, "start_model": model, "success": success, "cost_usd": cost, "input_tokens": 0,
            "output_tokens": 0, "iterations": 0, "escalations": 0, "router_task_type_ok": None,
            "router_domain_ok": None, "error": None, "expected_task_type": None, "task_type": None,
            "complexity": None}
    return {**base, **kw}


def test_section_14_example_sonnet_beats_k27_on_cost_per_success():
    """§14: K2.7 at $0.25 x 4 attempts = $1.00 per success; Sonnet at $0.80 x 1 = $0.80 per success."""
    rows = [row(model="tier3-code", success=False, cost=0.25) for _ in range(3)]
    rows += [row(model="tier3-code", success=True, cost=0.25)]
    rows += [row(model="tier5-sonnet", success=True, cost=0.80)]
    groups = by_category_model(rows)
    k27, sonnet = groups[("debugging", "tier3-code")], groups[("debugging", "tier5-sonnet")]
    assert k27.cost_per_success == pytest.approx(1.00)
    assert k27.success_rate == pytest.approx(0.25)
    assert sonnet.cost_per_success == pytest.approx(0.80)
    assert sonnet.cost_per_success < k27.cost_per_success


def test_averages_rates_and_router_accuracy():
    rows = [
        row(input_tokens=1000, output_tokens=500, iterations=3, escalations=1, router_task_type_ok=True,
            router_domain_ok=True, complexity="medium"),
        row(success=False, cost=0.1, input_tokens=3000, output_tokens=500, iterations=5, router_task_type_ok=False,
            router_domain_ok=True, complexity="hard"),
        row(error="HermesError: HTTP 500", cost=9.99, input_tokens=99999),
    ]
    st = by_category_model(rows)[("debugging", "tier3-code")]
    assert (st.runs, st.successes, st.errors) == (2, 1, 1)
    assert st.avg_tokens == pytest.approx(2500)
    assert st.avg_iterations == pytest.approx(4)
    assert st.escalation_rate == pytest.approx(0.5)
    assert st.router_task_type_accuracy == pytest.approx(0.5)
    assert st.router_domain_accuracy == pytest.approx(1.0)
    assert st.cost_per_success == pytest.approx(0.1)  # the errored row's $9.99 is excluded
    assert st.complexities == {"medium", "hard"}


def test_no_success_means_no_cost_per_success():
    st = Stats()
    st.add(row(success=False, cost=0.3))
    assert st.cost_per_success is None and st.success_rate == 0
    assert Stats().success_rate is None


def test_grouping_by_task_type_prefers_ground_truth():
    rows = [row(task_type="implementation", expected_task_type="debugging"), row(task_type="refactor")]
    assert set(by_task_type_model(rows)) == {("debugging", "tier3-code"), ("refactor", "tier3-code")}


def test_suggest_lowest_cost_per_success_above_target():
    rows = []
    rows += [row(model="tier2-flash", expected_task_type="summary", cost=0.01, complexity="simple") for _ in range(4)]
    rows += [row(model="tier2-flash", expected_task_type="summary", success=False, cost=0.01, complexity="simple")]
    rows += [row(model="tier3-code", expected_task_type="summary", cost=0.05, complexity="simple") for _ in range(5)]
    # cheapest of all but below 80% success: never suggested
    rows += [row(model="tier2-cheap", expected_task_type="summary", success=i < 2, cost=0.001) for i in range(5)]
    # too few runs
    rows += [row(model="local-qwen", expected_task_type="summary", cost=0.0)]
    [s] = suggest(rows, target_success=0.8, min_runs=3)
    assert (s.task_type, s.model, s.runs, s.complexities) == ("summary", "tier2-flash", 5, ("simple",))
    assert s.success_rate == pytest.approx(0.8)
    assert s.cost_per_success == pytest.approx(0.05 / 4)


def test_suggest_ignores_unknown_models_and_task_types():
    rows = [row(model="gpt-x", expected_task_type="summary") for _ in range(5)]
    rows += [row(model="tier3-code") for _ in range(5)]  # no task type at all
    assert suggest(rows, known_models={"tier3-code"}) == []


def test_table_renders_every_group():
    out = table(by_category_model([row(cost=0.5), row(category="finance", model="tier2-flash", cost=0.02)]),
                ("category", "start_model"))
    lines = out.splitlines()
    assert lines[0].split()[:3] == ["category", "start_model", "runs"]
    assert "$0.5000" in out and "100.0%" in out and lines[2].startswith("debugging")


def test_parse_since():
    now = datetime(2026, 10, 7, 12, tzinfo=UTC)
    assert parse_since("7d", now) == now - timedelta(days=7)
    assert parse_since("24h", now) == now - timedelta(hours=24)
    assert parse_since("2w", now) == now - timedelta(days=14)
    assert parse_since("2026-10-01", now) == datetime(2026, 10, 1, tzinfo=UTC)
    assert parse_since(None) is None
    with pytest.raises(ValueError):
        parse_since("ontem", now)
