"""Real Postgres round trip: rows written by PostgresStore come back, feed `bench report`, and the SQL view agrees.
Needs migrations/013_bench.sql applied; skipped unless BENCH_TEST_DATABASE_URL is set."""

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from conftest import ROUTING_FILE

from bench.cli import build_report
from bench.report import by_category_model
from bench.store import BenchRow, PostgresStore, StoreError

URL = os.environ.get("BENCH_TEST_DATABASE_URL")
pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(not URL, reason="BENCH_TEST_DATABASE_URL not set")]


@pytest.fixture
def store():
    s = PostgresStore(URL)
    batch = f"pytest-{uuid.uuid4().hex[:10]}"
    yield s, batch
    s.conn.execute("DELETE FROM bench_runs WHERE batch_id = %s", (batch,))
    s.close()


def make_row(batch, n, model, success, cost, **kw):
    return BenchRow(run_at=datetime.now(UTC), batch_id=batch, task_key="debug-go-desconto-centavos",
                    category="debugging", repeat_index=n, session_id=f"bench-{batch}-{n}", tenant="nitro",
                    success=success, check_type="command", duration_ms=1234 * n, agent="engineering",
                    hermes_profile="engineering", start_model=model, final_model=model, tier=3,
                    task_type="debugging", expected_task_type="debugging", domain="engineering",
                    expected_domain="engineering", complexity="medium", input_tokens=1000 * n, output_tokens=100,
                    cost_usd=cost, iterations=n, repairs=0, escalations=0, router_task_type_ok=True,
                    router_domain_ok=True, check_detail={"stage": "run", "exit_code": 0 if success else 1}, **kw)


def test_rows_round_trip_and_report(store):
    s, batch = store
    rows = [make_row(batch, 1, "tier3-code", False, 0.25), make_row(batch, 2, "tier3-code", False, 0.25),
            make_row(batch, 3, "tier3-code", False, 0.25), make_row(batch, 4, "tier3-code", True, 0.25),
            make_row(batch, 5, "tier5-sonnet", True, 0.80), make_row(batch, 6, "tier5-sonnet", True, 0.80),
            make_row(batch, 7, "tier5-sonnet", True, 0.80),
            make_row(batch, 8, "tier5-sonnet", False, 0.0, error="WorkspaceError: ssh failed")]
    for r in rows:
        s.insert(r)
    fetched = [r for r in s.fetch(datetime.now(UTC) - timedelta(minutes=5)) if r["batch_id"] == batch]
    assert len(fetched) == 8
    first = fetched[0]
    assert first["tenant"] == "nitro" and first["domain"] == "engineering"
    assert first["check_detail"] == {"stage": "run", "exit_code": 1} and isinstance(first["cost_usd"], float)
    assert first["run_at"].tzinfo is not None

    text, data = build_report(fetched, ROUTING_FILE.read_text(encoding="utf-8"), min_runs=3)
    groups = {(g["category"], g["start_model"]): g for g in data["by_category_model"]}
    assert groups[("debugging", "tier3-code")]["cost_per_success_usd"] == pytest.approx(1.00)
    assert groups[("debugging", "tier5-sonnet")]["cost_per_success_usd"] == pytest.approx(0.80)
    assert groups[("debugging", "tier5-sonnet")]["errors"] == 1
    assert [sg["model"] for sg in data["suggestions"]] == ["tier5-sonnet"]
    assert "debugging" in text and "$0.8000" in text

    # the SQL view and the Python report must agree on every (category, start model) in the table
    everything = by_category_model(s.fetch())
    view = s.conn.execute("SELECT category, start_model, runs, succeeded, cost_per_success_usd FROM v_bench_summary"
                          ).fetchall()
    assert view
    for category, model, runs, succeeded, cps in view:
        st = everything[(category, model or "unknown")]
        assert (runs, succeeded) == (st.runs, st.successes)
        assert (cps is None and st.cost_per_success is None) or float(cps) == pytest.approx(st.cost_per_success)


def test_nul_bytes_do_not_lose_the_row(store):
    s, batch = store
    row = make_row(batch, 1, "tier3-code", False, 0.1, error="WorkspaceError: \x00garbage")
    row.check_detail = {"output_tail": "panic\x00\x00", "answer_excerpt": "ok\x00", "literal": "\\u0000"}
    s.insert(row)
    [back] = [r for r in s.fetch() if r["batch_id"] == batch]
    assert back["error"] == "WorkspaceError: garbage"
    assert back["check_detail"] == {"output_tail": "panic", "answer_excerpt": "ok", "literal": "\\u0000"}


def test_duplicate_session_is_rejected(store):
    s, batch = store
    s.insert(make_row(batch, 1, "tier3-code", True, 0.1))
    with pytest.raises(StoreError):
        s.insert(make_row(batch, 1, "tier3-code", True, 0.1))


def test_missing_table_is_reported(monkeypatch):
    monkeypatch.setattr("bench.store.COLUMNS_SQL", "SELECT false")
    with pytest.raises(StoreError, match="013_bench"):
        PostgresStore(URL)
