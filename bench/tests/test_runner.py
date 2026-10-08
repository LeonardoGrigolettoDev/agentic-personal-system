import json
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import FIXTURES_DIR, SOLUTIONS_DIR
from stubs import DecisionHandler, DecisionState, HermesHandler, HermesState, serve

from bench.cli import main
from bench.decision import DecisionClient
from bench.hermes import INSTRUCTIONS, HermesClient
from bench.preflight import Preflight
from bench.runner import Runner, RunOptions, estimate_cost
from bench.store import MemoryStore
from bench.tasks import load_routing
from bench.workspace import CommandResult, LocalWorkspace

PRICES = load_routing(Path(__file__).parent / "data" / "routing.sample.yaml")["models"]
LEDGER = {"task_type": "debugging", "domain": "engineering", "complexity": "medium", "model": "tier4-pro",
          "start_model": "tier3-code", "tier": 4, "input_tokens": 12000, "output_tokens": 3000,
          "cache_read_tokens": 500, "cost_usd": 0.0734, "iterations": 4, "repairs": 1, "escalations": 1}


class FakeProbe:
    """Workspace stand-in for preflight probes only."""

    def __init__(self, exit_code, output=""):
        self.result, self.calls = CommandResult(exit_code, output), []

    def probe(self, command, env, timeout):
        self.calls.append((command, env))
        return self.result


class FixedJudge:
    def __init__(self, score):
        self.value = score

    def score(self, *, prompt, rubric, answer):
        return self.value, "ok"


@pytest.fixture
def stack(tmp_path):
    """Stub Hermes (default + engineering profile keys) and stub Decision Service, plus a Runner factory."""
    hermes = HermesState(keys={"": "hk", "/p/engineering": "ek"})
    decision = DecisionState(ledger=dict(LEDGER))
    with serve(HermesHandler, hermes) as h_url, serve(DecisionHandler, decision) as d_url:
        def make(fixtures_dir=FIXTURES_DIR, judge=None, task_timeout=30, decision_key="dk", preflight=None):
            store = MemoryStore()
            runner = Runner(fixtures_dir=fixtures_dir,
                            hermes=HermesClient(h_url, "hk", {"engineering": "ek"}, timeout=10, sleep=lambda s: None,
                                                stop_grace=0.3),
                            decision=DecisionClient(d_url, decision_key, timeout=10),
                            workspace=LocalWorkspace(tmp_path / "ws"), judge=judge, store=store,
                            task_timeout=task_timeout, poll_interval=0.01, prices=PRICES, preflight=preflight,
                            sleep=lambda s: None)
            return runner, store
        yield hermes, decision, make, (h_url, d_url)


def fix_workdir_with_solution(key):
    def on_submit(body):
        workdir = re.search(r"(/\S+/bench-" + re.escape(key) + r"-1-\d+)", body["input"]).group(1)
        shutil.copytree(SOLUTIONS_DIR / key, workdir, dirs_exist_ok=True)
    return on_submit


def test_debugging_task_end_to_end(stack, task_by_key, tmp_path):
    hermes, decision, make, _ = stack
    task = task_by_key["debug-py-paginacao"]
    hermes.on_submit = fix_workdir_with_solution(task.key)
    hermes.answer = "Corrigi o início da página (numero - 1) e o arredondamento para cima do total."
    runner, store = make()
    summary = runner.run([task], RunOptions(model="tier3-code"))

    [row] = store.rows
    assert row is summary.rows[0]
    assert row.success and row.error is None
    assert re.fullmatch(r"bench-debug-py-paginacao-1-\d{14}", row.session_id)
    # Hermes request: engineering profile prefix + its own key, session id, idempotency, model override
    path, headers, body = hermes.submits[0]
    assert path == "/p/engineering/v1/runs" and headers["Authorization"] == "Bearer ek"
    assert headers["Idempotency-Key"] == row.session_id == body["session_id"]
    assert body["model"] == "tier3-code" and body["instructions"] == INSTRUCTIONS
    assert "NÃO salve memórias" in INSTRUCTIONS and "NÃO crie tarefas Kanban" in INSTRUCTIONS
    assert str(tmp_path / "ws") in body["input"] and "{workdir}" not in body["input"]
    # pre-route with the task's real tenant and agent, then the ledger view, then the verdict
    assert decision.routes[0] == {"session_id": row.session_id, "text": body["input"], "tenant": "pessoal",
                                  "model": "tier3-code",
                                  "agent": "engineering"}
    assert decision.finishes == [(row.session_id, {"status": "succeeded", "task_type": "debugging"})]
    assert (row.start_model, row.final_model, row.tier) == ("tier3-code", "tier4-pro", 4)
    assert (row.input_tokens, row.output_tokens, row.cache_read_tokens) == (12000, 3000, 500)
    assert (row.cost_usd, row.iterations, row.repairs, row.escalations) == (0.0734, 4, 1, 1)
    # engineering pre-route: the Decision Service copies the agent's domain, so the router is not measured on it
    assert row.router_task_type_ok is True and row.router_domain_ok is None
    assert row.check_detail["domain_pinned_by_agent"] is True and row.check_detail["ledger_settle_reads"] == 1
    assert "model_override" not in row.check_detail
    assert row.tool_calls == 2 and row.hermes_status == "completed" and row.hermes_profile == "engineering"
    assert row.requested_model == "tier3-code" and row.duration_ms >= 0
    assert row.check_detail["stage"] == "run" and row.check_detail["protected"] == ["Makefile", "test_paginacao.py"]
    assert row.check_detail["answer_excerpt"].startswith("Corrigi")
    assert not any((tmp_path / "ws").iterdir())  # workdir cleaned up


def test_wrong_answer_is_a_failure_reported_to_routing(stack, task_by_key):
    hermes, decision, make, _ = stack
    decision.ledger.update(task_type="conversation", domain="finance")
    hermes.answer = "Dá uns R$ 11.200,00."
    runner, store = make()
    runner.run([task_by_key["simple-juros-compostos"]], RunOptions())
    [row] = store.rows
    assert not row.success and row.error is None
    path, headers, body = hermes.submits[0]
    assert path == "/v1/runs" and headers["Authorization"] == "Bearer hk" and "model" not in body
    assert row.hermes_profile == "default" and decision.routes[0]["agent"] == "chief"
    assert decision.finishes[0][1] == {"status": "failed", "task_type": "finance_analysis"}
    assert row.router_task_type_ok is False and row.router_domain_ok is True  # chief: domain came from the router
    assert "domain_pinned_by_agent" not in row.check_detail


def test_approval_requests_are_denied(stack, task_by_key):
    hermes, _, make, _ = stack
    hermes.ask_approval = True
    hermes.answer = "`git reset --soft HEAD~1` desfaz o commit e mantém as mudanças."
    runner, store = make()
    runner.run([task_by_key["simple-git-desfaz-commit"]], RunOptions())
    [row] = store.rows
    assert row.success
    assert hermes.approvals[0] == {"choice": "deny", "all": True}
    assert row.check_detail["hermes"]["approvals_denied"] >= 1


def test_busy_hermes_is_retried_with_the_same_idempotency_key(stack, task_by_key):
    hermes, _, make, _ = stack
    hermes.throttle = 2
    hermes.answer = "409 Conflict"
    runner, store = make()
    runner.run([task_by_key["simple-http-status-estoque"]], RunOptions())
    assert store.rows[0].success
    keys = {h["Idempotency-Key"] for _, h, _ in hermes.submits}
    assert len(hermes.submits) == 3 and len(keys) == 1


def test_hermes_failure_is_an_infrastructure_error(stack, task_by_key):
    hermes, decision, make, _ = stack
    hermes.submit_error = 400
    runner, store = make()
    summary = runner.run([task_by_key["simple-cron-dias-uteis"]], RunOptions())
    [row] = store.rows
    assert not row.success and "HTTP 400" in row.error and summary.errors == 1
    assert decision.finishes[0][1]["status"] == "cancelled"  # errors never teach routing a failure
    assert row.check_detail == {"evaluated": False, "ledger_settle_reads": 1, "ledger_finish": "cancelled"}


@pytest.mark.parametrize("status, error", [("failed", "HTTP 400: Budget has been exceeded"),
                                           ("interrupted", "The gateway restarted before this run settled."),
                                           ("cancelled", None)])
def test_hermes_run_without_a_verdict_is_cancelled_not_failed(stack, task_by_key, status, error):
    hermes, decision, make, _ = stack
    hermes.final_status, hermes.final_error = status, error
    runner, store = make()
    summary = runner.run([task_by_key["simple-cron-dias-uteis"]], RunOptions())
    [row] = store.rows
    assert not row.success and row.hermes_status == status and summary.errors == 1
    assert row.error.startswith(f"hermes {status}: ") and (error or "without an answer") in row.error
    assert decision.finishes[0][1]["status"] == "cancelled"  # LiteLLM/budget outages never teach routing a failure
    assert row.check_detail["evaluated"] is False


def test_failed_without_error_is_the_agents_failure(stack, task_by_key):
    hermes, decision, make, _ = stack
    hermes.final_status = "failed"  # e.g. iteration budget: turn_exit_reason, no error
    runner, store = make()
    runner.run([task_by_key["simple-cron-dias-uteis"]], RunOptions())
    [row] = store.rows
    assert row.error is None and not row.success and decision.finishes[0][1]["status"] == "failed"


def test_ledger_is_reread_until_the_last_usage_lands(stack, task_by_key):
    hermes, decision, make, _ = stack
    decision.ledger.update(input_tokens=300, output_tokens=40, llm_calls=1, cost_usd=0.001)
    decision.get_updates = [{}, {"input_tokens": 950, "output_tokens": 130, "llm_calls": 2, "cost_usd": 0.002}]
    hermes.answer = "`30 7 * * 1-5`"
    runner, store = make()
    runner.run([task_by_key["simple-cron-dias-uteis"]], RunOptions())
    [row] = store.rows
    assert (row.input_tokens, row.output_tokens, row.cost_usd) == (950, 130, 0.002)
    assert row.check_detail["ledger_settle_reads"] == 2 and decision.gets == 2


def test_ledger_reread_stops_when_llm_calls_are_stable(stack, task_by_key):
    hermes, decision, make, _ = stack
    decision.ledger.update(input_tokens=300, llm_calls=3)  # below Hermes' 900, but nothing else arrives
    hermes.answer = "`30 7 * * 1-5`"
    runner, store = make()
    runner.run([task_by_key["simple-cron-dias-uteis"]], RunOptions())
    assert store.rows[0].check_detail["ledger_settle_reads"] == 2 and store.rows[0].input_tokens == 300


def test_model_is_pinned_through_the_pre_route(stack, task_by_key):
    hermes, decision, make, _ = stack
    hermes.answer = "`30 7 * * 1-5`"
    runner, store = make()
    runner.run([task_by_key["simple-cron-dias-uteis"]], RunOptions(model="tier2-cheap"))
    [row] = store.rows
    assert decision.routes[0]["model"] == "tier2-cheap"
    assert row.requested_model == row.start_model == "tier2-cheap" and "model_override" not in row.check_detail


def test_ignored_model_override_is_flagged(stack, task_by_key):
    hermes, decision, make, _ = stack
    decision.honor_model = False  # older Decision Service: the routed model wins
    hermes.answer = "`30 7 * * 1-5`"
    runner, store = make()
    runner.run([task_by_key["simple-cron-dias-uteis"]], RunOptions(model="tier2-cheap"))
    [row] = store.rows
    assert row.requested_model == "tier2-cheap" and row.start_model == "tier3-code"
    assert row.check_detail["model_override"] == "ignored"


def test_rejected_model_pin_is_an_error_not_a_run(stack, task_by_key):
    hermes, decision, make, _ = stack
    decision.reject_model = True
    runner, store = make()
    runner.run([task_by_key["simple-cron-dias-uteis"]], RunOptions(model="tier7-fable"))
    [row] = store.rows
    assert row.error and "pinning tier7-fable" in row.error and not row.success
    assert hermes.submits == []  # never sent to Hermes unpinned


def test_without_decision_service_tokens_come_from_hermes(stack, task_by_key):
    hermes, decision, make, _ = stack
    decision.down = True
    hermes.answer = "`30 7 * * 1-5`"
    runner, store = make()
    runner.run([task_by_key["simple-cron-dias-uteis"]], RunOptions(model="tier2-cheap"))
    [row] = store.rows
    assert row.success and row.error is None
    assert (row.input_tokens, row.output_tokens) == (900, 120)
    assert row.check_detail["ledger"] == "unavailable" and decision.finishes == []
    assert row.start_model == "tier2-cheap" and row.final_model == "tier2-cheap"
    assert row.cost_usd == pytest.approx((900 * 0.03 + 120 * 0.13) / 1e6)
    assert row.check_detail["usage_source"] == "hermes"


def test_ledger_without_usage_falls_back_to_hermes_counters(stack, task_by_key):
    hermes, decision, make, _ = stack
    decision.ledger.update(input_tokens=0, output_tokens=0, cost_usd=0, model="tier3-code")
    hermes.answer = "`30 7 * * 1-5`"
    runner, store = make()
    runner.run([task_by_key["simple-cron-dias-uteis"]], RunOptions())
    [row] = store.rows
    assert (row.input_tokens, row.output_tokens) == (900, 120) and row.final_model == "tier3-code"
    assert row.cost_usd == pytest.approx((900 * 0.95 + 120 * 4.0) / 1e6)
    assert decision.finishes[0][1]["status"] == "succeeded"


def test_estimate_cost_matches_the_decision_service_formula():
    # fresh input at the input price, cached input at cache_read, output at the output price (USD per 1M)
    assert estimate_cost(PRICES, "tier3-code", 1000, 100, 200) == pytest.approx((800 * 0.95 + 200 * 0.19 + 100 * 4) / 1e6)
    assert estimate_cost(PRICES, "tier2-cheap", 1000, 0, 500) == pytest.approx(1000 * 0.03 / 1e6)  # no cache price
    assert estimate_cost(PRICES, "gpt-x", 1000, 100, 0) is None


def test_event_stream_unavailable_falls_back_to_polling(stack, task_by_key):
    hermes, _, make, _ = stack
    hermes.events_status = 404
    hermes.answer = "`30 7 * * MON-FRI`"
    runner, store = make()
    runner.run([task_by_key["simple-cron-dias-uteis"]], RunOptions())
    assert store.rows[0].success and store.rows[0].tool_calls is None


def test_timeout_stops_the_run_and_never_judges_it(stack, task_by_key):
    hermes, decision, make, _ = stack
    hermes.never_finish, hermes.events_status = True, 404
    hermes.on_submit = fix_workdir_with_solution("debug-py-paginacao")  # a passing tree must not count after timeout
    runner, store = make(task_timeout=0.3)
    runner.run([task_by_key["debug-py-paginacao"]], RunOptions())
    [row] = store.rows
    assert row.hermes_status == "timeout" and not row.success and row.error is None
    assert row.check_detail["reason"] == "timeout" and row.check_detail["evaluated"] is False
    assert "stopped; run ended cancelled" in row.check_detail["hermes"]["error"]
    assert hermes.stops and decision.finishes[0][1]["status"] == "failed"
    assert "workdir_kept" not in row.check_detail


def test_workdir_is_kept_while_a_stopped_run_is_still_running(stack, task_by_key, tmp_path):
    hermes, _, make, _ = stack
    hermes.never_finish, hermes.ignore_stop, hermes.events_status = True, True, 404
    runner, store = make(task_timeout=0.2)
    runner.run([task_by_key["debug-py-paginacao"]], RunOptions())
    [row] = store.rows
    assert row.hermes_status == "timeout" and row.error is None and "still running" in row.check_detail["hermes"]["error"]
    kept = Path(row.check_detail["workdir_kept"])
    assert kept.is_dir() and kept.parent == tmp_path / "ws"


def test_llm_judge_task(stack, task_by_key):
    hermes, _, make, _ = stack
    hermes.answer = "1. 08:30 pagar a fatura ..."
    runner, store = make(judge=FixedJudge(8))
    runner.run([task_by_key["simple-prioriza-tarefas"]], RunOptions())
    assert store.rows[0].success and store.rows[0].check_detail["score"] == 8
    runner, store = make(judge=None)
    runner.run([task_by_key["simple-prioriza-tarefas"]], RunOptions())
    assert store.rows[0].error and not store.rows[0].success


def test_repeat_and_missing_generated_media_are_skipped(stack, task_by_key, tmp_path):
    hermes, _, make, _ = stack
    fixtures = tmp_path / "fixtures"
    shutil.copytree(FIXTURES_DIR / "media-recado-whatsapp", fixtures / "media-recado-whatsapp",
                    ignore=shutil.ignore_patterns("*.wav"))
    hermes.answer = "`30 7 * * 1-5`"
    runner, store = make(fixtures_dir=fixtures)
    summary = runner.run([task_by_key["media-recado-whatsapp"], task_by_key["simple-cron-dias-uteis"]],
                         RunOptions(repeat=2))
    assert [k for k, _ in summary.skipped] == ["media-recado-whatsapp"]
    assert [r.repeat_index for r in store.rows] == [1, 2]
    assert store.rows[0].session_id != store.rows[1].session_id


def test_tasks_whose_service_is_unreachable_are_skipped(stack, task_by_key, tmp_path):
    hermes, _, make, _ = stack
    fixtures = tmp_path / "fixtures"
    (fixtures / "media-recado-whatsapp").mkdir(parents=True)
    (fixtures / "media-recado-whatsapp" / "recado.wav").write_bytes(b"RIFF")
    why = "EDGE_API_KEY does not reach the terminal (sandbox sshd needs AcceptEnv EDGE_API_KEY)"
    probe = FakeProbe(3, why + "\n")
    hermes.answer = "`30 7 * * 1-5`"
    runner, store = make(fixtures_dir=fixtures, preflight=Preflight(probe))
    task = task_by_key["media-recado-whatsapp"]
    summary = runner.run([task, task_by_key["media-aula-concorrencia"], task_by_key["simple-cron-dias-uteis"]],
                         RunOptions())
    assert summary.skipped[0] == ("media-recado-whatsapp", f"requires edge: {why}")
    assert summary.skipped[1][1].startswith("missing generated files aula.wav")
    assert [r.task_key for r in store.rows] == ["simple-cron-dias-uteis"] and not hermes.submits[1:]
    assert len(probe.calls) == 1 and probe.calls[0][1] == {"EDGE_API_KEY": "bench-probe"}
    runner, store = make(fixtures_dir=fixtures, preflight=Preflight(FakeProbe(0)))
    hermes.answer = "Fernanda: quinta-feira às 15h, levar o relatório de bugs"
    assert runner.run([task], RunOptions()).skipped == [] and store.rows[0].success


def test_cli_dry_run_prints_the_plan(capsys, monkeypatch):
    monkeypatch.setenv("BENCH_ENV_FILE", "/nonexistent")
    assert main(["run", "--dry-run", "--task", "debug-go-desconto-centavos", "--repeat", "2"]) == 0
    plan = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [p["session_id"].rsplit("-", 2)[1] for p in plan] == ["1", "2"]
    assert plan[0]["hermes_profile"] == "default" and plan[0]["check"] == "command"


def test_cli_rejects_unknown_model(capsys, monkeypatch):
    monkeypatch.setenv("BENCH_ENV_FILE", "/nonexistent")
    monkeypatch.delenv("DECISION_API_KEY", raising=False)
    assert main(["run", "--dry-run", "--model", "gpt-9"]) == 2
    assert "not a model alias" in capsys.readouterr().err
    assert main(["run", "--dry-run", "--model", "tier2-cheap", "--task", "simple-cron-dias-uteis"]) == 0


def test_cli_accepts_model_with_the_decision_service(monkeypatch):
    monkeypatch.setenv("BENCH_ENV_FILE", "/nonexistent")
    monkeypatch.setenv("DECISION_API_KEY", "dk")
    assert main(["run", "--dry-run", "--model", "tier2-cheap", "--task", "simple-cron-dias-uteis"]) == 0


def test_cli_run_local_no_db_against_stubs(stack, capsys, monkeypatch, tmp_path):
    hermes, decision, _, (h_url, d_url) = stack
    hermes.answer = "`30 7 * * 1-5`"
    for k, v in {"BENCH_ENV_FILE": "/nonexistent", "AIOS_HERMES_URL": h_url, "HERMES_API_KEY": "hk",
                 "AIOS_DECISION_URL": d_url, "DECISION_API_KEY": "dk", "BENCH_WORKSPACE": str(tmp_path / "w"),
                 "BENCH_POLL_INTERVAL": "0.01"}.items():
        monkeypatch.setenv(k, v)
    assert main(["--log-format", "text", "run", "--task", "simple-cron-dias-uteis", "--local", "--no-db"]) == 0
    row = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert row["success"] is True and row["task_key"] == "simple-cron-dias-uteis"
    assert datetime.fromisoformat(row["run_at"]).tzinfo == UTC
    assert decision.finishes[0][1]["status"] == "succeeded"


def test_cli_validate_and_list(capsys, monkeypatch):
    monkeypatch.setenv("BENCH_ENV_FILE", "/nonexistent")
    assert main(["validate"]) == 0
    assert capsys.readouterr().out.strip().endswith("ok")
    assert main(["list", "--category", "media"]) == 0
    assert len(capsys.readouterr().out.strip().splitlines()) == 5


def test_cli_fixtures_without_tools(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("BENCH_ENV_FILE", "/nonexistent")
    monkeypatch.setenv("BENCH_FIXTURES_DIR", str(tmp_path))
    monkeypatch.setattr("bench.fixtures_gen.shutil.which", lambda name: None)
    assert main(["fixtures"]) == 0
    out = capsys.readouterr().out
    assert "created  media-metadados-audio/tom.wav" in out and "skipped" in out
    assert Path(tmp_path / "media-metadados-audio" / "tom.wav").stat().st_size == 12 * 16000 * 2 + 44
