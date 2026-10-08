import json

import httpx
import pytest

from bench.checks import (
    CheckResult,
    JudgeError,
    check_json_schema,
    evaluate,
    extract_json,
    normalize_numbers,
    protect_command,
    sha256_files,
)
from bench.judge import LiteLLMJudge, parse_verdict
from bench.workspace import LocalWorkspace, WorkspaceError


@pytest.mark.parametrize("text, want", [
    ("R$ 1.234,56", "R$ 1234.56"),
    ("total 11.268,25 reais", "total 11268.25 reais"),
    ("1,234.56 USD", "1234.56 USD"),
    ("R$4.200", "R$4200"),
    ("12,5% ao ano", "12.5% ao ano"),
    ("4200.00 e 0.300", "4200.00 e 0.300"),
    ("itens 1,2,3", "itens 1,2,3"),
    ("1.234.567,89", "1234567.89"),
    ("v1.22.3", "v1.22.3"),
])
def test_normalize_numbers(text, want):
    assert normalize_numbers(text) == want


def test_extract_json_prefers_fenced_then_whole_then_embedded():
    assert extract_json('Aqui:\n```json\n{"a": 1}\n```\nfim') == {"a": 1}
    assert extract_json('  [1, 2] ') == [1, 2]
    assert extract_json('Resultado: {"b": [1, {"c": 2}]} ok.') == {"b": [1, {"c": 2}]}
    with pytest.raises(ValueError):
        extract_json("sem json aqui")


def test_regex_check_with_normalisation():
    check = {"type": "regex", "pattern": r"\b11268\.25\b", "normalize_numbers": True}
    assert evaluate(check, answer="Fica R$ 11.268,25.").passed
    res = evaluate({"type": "regex", "pattern": r"\b11268\.25\b"}, answer="Fica R$ 11.268,25.")
    assert not res.passed and res.detail["type"] == "regex"


def test_json_schema_check_picks_the_candidate_that_validates():
    check = {"type": "json_schema", "schema": {"type": "object", "required": ["total"],
                                                "properties": {"total": {"const": 3}}}}
    answer = 'Primeiro {"rascunho": true} e depois o final: {"total": 3}'
    res = evaluate(check, answer=answer)
    assert res.passed and json.loads(res.detail["value"]) == {"total": 3}
    bad = evaluate(check, answer='{"total": 4}')
    assert not bad.passed and "total" in bad.detail["errors"][0]
    none = check_json_schema(check, "nada")
    assert not none.passed and "no JSON" in none.detail["reason"]


def test_empty_answer_fails_without_calling_the_judge():
    class Boom:
        def score(self, **_):
            raise AssertionError("judge must not be called")

    res = evaluate({"type": "llm_judge", "rubric": "r" * 20, "pass_score": 5}, answer="  ", judge=Boom())
    assert not res.passed and res.error is None and res.detail["reason"] == "empty final answer"


class FixedJudge:
    def __init__(self, score=None, error=None):
        self._score, self._error, self.calls = score, error, []

    def score(self, *, prompt, rubric, answer):
        self.calls.append((prompt, rubric, answer))
        if self._error:
            raise JudgeError(self._error)
        return self._score, "justificativa"


def test_llm_judge_pass_fail_and_error():
    check = {"type": "llm_judge", "rubric": "critério " * 5, "pass_score": 7}
    judge = FixedJudge(score=8)
    assert evaluate(check, answer="resposta", prompt="tarefa", judge=judge).passed
    assert judge.calls == [("tarefa", check["rubric"], "resposta")]
    assert not evaluate(check, answer="resposta", judge=FixedJudge(score=6.9)).passed
    err = evaluate(check, answer="resposta", judge=FixedJudge(error="HTTP 503"))
    assert not err.passed and "HTTP 503" in err.error
    assert evaluate(check, answer="resposta", judge=None).error


def test_command_check_exit_codes_timeout_and_protection(tmp_path):
    fixture = tmp_path / "fx"
    fixture.mkdir()
    (fixture / "t_test.go").write_text("teste original\n")
    ws = LocalWorkspace(tmp_path / "ws")
    workdir = ws.stage(fixture, "bench-x-1-1")
    hashes = sha256_files(fixture, ["t_test.go"])

    ok = evaluate({"type": "command", "run": "test -f t_test.go && echo pronto"}, answer="", workspace=ws,
                  workdir=workdir, protect_hashes=hashes)
    assert ok.passed and "pronto" in ok.detail["output_tail"] and ok.detail["protected"] == ["t_test.go"]

    assert evaluate({"type": "command", "run": "exit 3", "expect_exit": 3}, answer="", workspace=ws,
                    workdir=workdir).passed
    slow = evaluate({"type": "command", "run": "sleep 5", "timeout_s": 1}, answer="", workspace=ws, workdir=workdir)
    assert not slow.passed and slow.detail["timed_out"]

    (tmp_path / "ws" / "bench-x-1-1" / "t_test.go").write_text("teste alterado pelo agente\n")
    tampered = evaluate({"type": "command", "run": "true"}, answer="", workspace=ws, workdir=workdir,
                        protect_hashes=hashes)
    assert not tampered.passed and tampered.detail["stage"] == "protect"

    missing = evaluate({"type": "command", "run": "true"}, answer="", workspace=None, workdir=None)
    assert missing.error


def test_protect_command_quotes_paths():
    cmd = protect_command({"src/a b.ts": "ab" * 32})
    assert "sha256sum -c --quiet -" in cmd and "'" in cmd


def test_local_workspace_refuses_paths_outside_base(tmp_path):
    ws = LocalWorkspace(tmp_path / "ws")
    with pytest.raises(WorkspaceError):
        ws.stage(None, "../fora")
    with pytest.raises(WorkspaceError):
        ws.remove(str(tmp_path))
    workdir = ws.stage(None, "bench-ok")
    ws.remove(workdir)
    assert not (tmp_path / "ws" / "bench-ok").exists()


def test_check_result_defaults():
    assert CheckResult(True).detail == {} and CheckResult(True).error is None


# ---------------------------------------------------------------- judge client

@pytest.mark.parametrize("content, score", [
    ('{"score": 8, "justificativa": "ok"}', 8.0),
    ('Avaliação:\n```json\n{"score": 6.5, "justificativa": "faltou fonte"}\n```', 6.5),
    ('nota final "score": 9 de 10', 9.0),
])
def test_parse_verdict(content, score):
    assert parse_verdict(content)[0] == score


@pytest.mark.parametrize("content", ["sem nota", '{"score": 15}'])
def test_parse_verdict_rejects_garbage(content):
    with pytest.raises(JudgeError):
        parse_verdict(content)


def _judge(handler) -> LiteLLMJudge:
    j = LiteLLMJudge("http://litellm:4000", "sk-bench", "tier5-sonnet",
                     client=httpx.Client(transport=httpx.MockTransport(handler)))
    return j


def test_litellm_judge_request_and_retry(monkeypatch):
    monkeypatch.setattr("bench.judge.time.sleep", lambda s: None)
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if len(seen) == 1:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"score": 9, "justificativa": "bom"}'}}]})

    score, reason = _judge(handler).score(prompt="p", rubric="r", answer="a")
    assert (score, reason) == (9.0, "bom")
    body = json.loads(seen[-1].content)
    assert seen[-1].url.path == "/v1/chat/completions"
    assert seen[-1].headers["authorization"] == "Bearer sk-bench"
    assert body["model"] == "tier5-sonnet" and body["temperature"] == 0


def test_litellm_judge_client_error_is_not_retried():
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(401, text="bad key")

    with pytest.raises(JudgeError, match="401"):
        _judge(handler).score(prompt="p", rubric="r", answer="a")
    assert len(calls) == 1


def test_litellm_judge_needs_a_key():
    with pytest.raises(ValueError):
        LiteLLMJudge("http://x", "", "m")
