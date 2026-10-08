import json

import httpx
import pytest
import respx

from edge.errors import UpstreamError
from edge.llm import LLMClient
from edge.summarize import (
    CHARS_PER_TOKEN,
    LOCAL_LIMITS,
    NOTE_WORDS,
    PROMPT_RESERVE,
    REMOTE_LIMITS,
    RETRY_JSON,
    RETRY_SHORTER,
    TASK_WORDS,
    TOPIC_WORDS,
    Limits,
    Run,
    Summarizer,
    _batches,
    _compact,
    estimate_tokens,
    map_messages,
    normalize,
    prompt_tokens,
    reduce_messages,
    render_markdown,
    split_text,
    with_note,
)

from .conftest import GOOD_SUMMARY, LITELLM, chat_response, put_media

CHAT = f"{LITELLM}/v1/chat/completions"


def sentences(n: int) -> str:
    return " ".join(f"Frase número {i} sobre o projeto de migração do banco de dados." for i in range(n))


def make_summarizer(**kw) -> Summarizer:
    opts = {
        "default_model": "local-qwen",
        "fallback_model": "tier2-cheap",
        "map_chunk_tokens": 2500,
        "fallback_chunk_tokens": 24000,
        "local_max_tokens": 24000,
    } | kw
    return Summarizer(LLMClient(LITELLM, "k", 5, backoff=0), **opts)


def payload(call) -> dict:
    return json.loads(call.request.content)


# ---------------------------------------------------------------- chunking
def test_chunks_respect_budget_and_keep_every_word():
    text = "\n\n".join(sentences(40) for _ in range(6))
    chunks = split_text(text, 500)
    assert len(chunks) > 1
    assert all(len(c) <= 500 * CHARS_PER_TOKEN for c in chunks)
    assert " ".join(" ".join(chunks).split()) == " ".join(text.split())


def test_chunks_split_long_lines_and_words():
    text = "palavra " * 3000 + "x" * 5000
    chunks = split_text(text, 300)
    assert all(len(c) <= 300 * CHARS_PER_TOKEN for c in chunks)
    assert "".join(chunks).replace(" ", "") == text.replace(" ", "")


def test_small_text_is_one_chunk_and_blank_is_none():
    assert split_text("Olá.", 2500) == ["Olá."]
    assert split_text("   \n ", 2500) == []


# ---------------------------------------------------------------- normalization
def test_normalize_accepts_portuguese_keys_and_messy_items():
    out = normalize(
        {
            "resumo": "R",
            "notas": "- um\n- dois\n- um",
            "tarefas": ["Ligar", {"titulo": "Enviar", "prazo": "null", "responsável": "Ana"}],
            "temas": ["a", "A", ""],
        },
        "all",
    )
    assert out == {
        "summary": "R",
        "notes": ["um", "dois"],
        "tasks": [{"title": "Ligar"}, {"title": "Enviar", "owner": "Ana"}],
        "topics": ["a"],
    }


def test_normalize_keeps_only_requested_kind():
    assert normalize(GOOD_SUMMARY, "tasks") == {
        "summary": "",
        "notes": [],
        "topics": [],
        "tasks": [{"title": "Preparar o backup", "due": "sexta", "owner": "João"}],
    }


# ---------------------------------------------------------------- map-reduce
@respx.mock
def test_single_chunk_is_one_call():
    route = respx.post(CHAT).mock(return_value=chat_response(json.dumps(GOOD_SUMMARY)))
    result = make_summarizer().summarize("Texto curto da reunião.", "all")
    assert route.call_count == 1
    body = payload(route.calls.last)
    assert body["model"] == "local-qwen" and body["response_format"] == {"type": "json_object"}
    assert "português do Brasil" in body["messages"][0]["content"]
    assert result["tasks"] == GOOD_SUMMARY["tasks"]
    assert result["meta"]["chunks"] == 1 and result["meta"]["llm_calls"] == 1
    assert result["meta"]["fallback_used"] is False


@respx.mock
def test_map_reduce_for_long_text():
    route = respx.post(CHAT).mock(return_value=chat_response(json.dumps(GOOD_SUMMARY)))
    text = sentences(400)  # ~26k chars -> several chunks at 2500 tokens
    result = make_summarizer().summarize(text, "all")
    n_chunks = len(split_text(text, 2500))
    assert n_chunks >= 3
    prompts = [payload(c)["messages"][1]["content"] for c in route.calls]
    maps = [p for p in prompts if p.startswith("Este é o trecho")]
    reduces = [p for p in prompts if "PARCIAIS" in p]
    assert len(maps) == n_chunks and len(reduces) >= 1
    assert all(payload(c)["max_tokens"] <= LOCAL_LIMITS["reduce"].max_tokens for c in route.calls)
    for p in maps:  # every map prompt fits the local 4K context
        assert estimate_tokens(p) < 2500 + 600
    assert result["meta"]["chunks"] == n_chunks and result["meta"]["llm_calls"] == len(prompts)


@respx.mock
def test_reduce_is_hierarchical_when_partials_do_not_fit():
    big = {**GOOD_SUMMARY, "summary": "resumo parcial " * 120}
    route = respx.post(CHAT).mock(return_value=chat_response(json.dumps(big)))
    make_summarizer(map_chunk_tokens=400).summarize(sentences(300), "summary")
    reduce_calls = [c for c in route.calls if "PARCIAIS" in payload(c)["messages"][1]["content"]]
    assert len(reduce_calls) > 1


@respx.mock
def test_bad_json_is_retried_once_then_accepted():
    route = respx.post(CHAT).mock(
        side_effect=[
            chat_response("Claro! Segue o resumo: a reunião foi boa."),
            chat_response("```json\n" + json.dumps(GOOD_SUMMARY) + "\n```"),
        ]
    )
    result = make_summarizer().summarize("Texto.", "all")
    assert route.call_count == 2
    first, retry = (payload(c) for c in route.calls)
    # same prompt plus a note, no extra turns: the retry fits the context reserved for the first try
    assert len(retry["messages"]) == 2 and "Claro!" not in json.dumps(retry["messages"])
    assert retry["messages"][1]["content"] == first["messages"][1]["content"] + "\n\n" + RETRY_JSON
    assert retry["temperature"] == 0.0
    assert result["summary"] == GOOD_SUMMARY["summary"]


@respx.mock
def test_fallback_after_two_bad_answers_from_local():
    def answer(request):
        model = json.loads(request.content)["model"]
        return chat_response("não sei" if model == "local-qwen" else json.dumps(GOOD_SUMMARY), model)

    route = respx.post(CHAT).mock(side_effect=answer)
    result = make_summarizer().summarize("Texto.", "all")
    assert [payload(c)["model"] for c in route.calls] == ["local-qwen", "local-qwen", "tier2-cheap"]
    assert result["meta"]["fallback_used"] is True and result["meta"]["models_used"] == ["tier2-cheap"]


@respx.mock
def test_local_outage_switches_the_rest_of_the_job_to_fallback():
    def answer(request):
        model = json.loads(request.content)["model"]
        if model == "local-qwen":
            return httpx.Response(400, json={"error": "context window exceeded"})
        return chat_response(json.dumps(GOOD_SUMMARY), model)

    route = respx.post(CHAT).mock(side_effect=answer)
    make_summarizer().summarize(sentences(400), "all")
    models = [payload(c)["model"] for c in route.calls]
    assert models.count("local-qwen") == 1  # sticky: no further attempts on the failed primary
    assert set(models[1:]) == {"tier2-cheap"}


@respx.mock
def test_huge_text_goes_straight_to_fallback_with_big_chunks():
    route = respx.post(CHAT).mock(return_value=chat_response(json.dumps(GOOD_SUMMARY), "tier2-cheap"))
    text = sentences(1200)  # ~79k chars ~ 25k tokens > EDGE_LOCAL_MAX_TOKENS
    result = make_summarizer().summarize(text, "all")
    assert {payload(c)["model"] for c in route.calls} == {"tier2-cheap"}
    assert result["meta"]["chunks"] == len(split_text(text, 24000))


@respx.mock
def test_everything_failing_raises_502():
    respx.post(CHAT).mock(return_value=chat_response("nada de json"))
    with pytest.raises(UpstreamError, match="local-qwen, tier2-cheap"):
        make_summarizer().summarize("Texto.", "all")


@respx.mock
def test_no_fallback_configured():
    route = respx.post(CHAT).mock(return_value=httpx.Response(503, text="down"))
    with pytest.raises(UpstreamError):
        make_summarizer(fallback_model=None).summarize("Texto.", "notes")
    assert {payload(c)["model"] for c in route.calls} == {"local-qwen"}


# ---------------------------------------------------------------- HTTP
@respx.mock
def test_summarize_endpoint_with_text_and_kind(client):
    route = respx.post(CHAT).mock(return_value=chat_response(json.dumps({"topics": ["backup", "migração"]})))
    resp = client.post("/summarize", json={"text": "João fará o backup.", "kind": "topics"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["topics"] == ["backup", "migração"] and body["summary"] == "" and body["tasks"] == []
    assert route.calls.last.request.headers["authorization"] == "Bearer litellm-edge-key"
    prompt = payload(route.calls.last)["messages"][1]["content"]
    assert '"topics"' in prompt and '"tasks"' not in prompt


@respx.mock
def test_summarize_endpoint_from_transcript_uri(client, settings):
    respx.post(CHAT).mock(return_value=chat_response(json.dumps(GOOD_SUMMARY)))
    uri = put_media(settings, "transcripts/abc.txt", "Reunião sobre backup.".encode())
    body = client.post("/summarize", json={"transcript_uri": uri, "model": "tier2-flash"}).json()
    assert body["transcript_uri"] == uri and body["meta"]["model"] == "tier2-flash"


def test_summarize_needs_exactly_one_input(client):
    assert client.post("/summarize", json={}).status_code == 422
    assert client.post("/summarize", json={"text": "a", "transcript_uri": "storage://media/x.txt"}).status_code == 422
    assert client.post("/summarize", json={"text": "a", "kind": "poem"}).status_code == 422


def test_blank_text_needs_no_llm(client):
    body = client.post("/summarize", json={"text": "   "}).json()
    assert body["summary"] == "" and body["meta"]["llm_calls"] == 0


def test_markdown_rendering():
    md = render_markdown(GOOD_SUMMARY, "Resumo: Reunião")
    assert md.startswith("# Resumo: Reunião\n\n## Resumo\n")
    assert "- [ ] Preparar o backup (responsável: João; prazo: sexta)" in md
    assert md.endswith("backup, banco de dados\n")


# ---------------------------------------------------------------- output budget (truncation, context)
CUT = '{"summary": "Reunião de status.", "notes": ["Backup ok"], "tasks": [{"title": "Enviar relat'


@respx.mock
def test_cut_answer_is_never_accepted_and_is_asked_again_shorter():
    route = respx.post(CHAT).mock(
        side_effect=[chat_response(CUT, finish_reason="length"), chat_response(json.dumps(GOOD_SUMMARY))]
    )
    result = make_summarizer().summarize("Texto da reunião.", "all")
    assert result["tasks"] == GOOD_SUMMARY["tasks"] and "Enviar relat" not in json.dumps(result)
    first, retry = (payload(c) for c in route.calls)
    assert retry["max_tokens"] == first["max_tokens"] and retry["temperature"] == 0.0
    shorter = LOCAL_LIMITS["map"].shorter()
    assert f"no máximo {shorter.notes} strings" in retry["messages"][1]["content"]
    assert retry["messages"][1]["content"].endswith(RETRY_SHORTER) and "Enviar relat" not in json.dumps(retry)


@respx.mock
def test_answer_cut_twice_goes_to_the_fallback_with_a_bigger_budget():
    def answer(request):
        model = json.loads(request.content)["model"]
        if model == "local-qwen":
            return chat_response(CUT, model, finish_reason="length")
        return chat_response(json.dumps(GOOD_SUMMARY), model)

    route = respx.post(CHAT).mock(side_effect=answer)
    result = make_summarizer().summarize("Texto da reunião.", "all")
    calls = [payload(c) for c in route.calls]
    assert [c["model"] for c in calls] == ["local-qwen", "local-qwen", "tier2-cheap"]
    assert calls[-1]["max_tokens"] == REMOTE_LIMITS["map"].max_tokens
    assert result["tasks"] == GOOD_SUMMARY["tasks"] and result["meta"]["fallback_used"] is True


@respx.mock
def test_cut_answer_without_fallback_fails_instead_of_returning_partial_data():
    respx.post(CHAT).mock(return_value=chat_response(CUT, finish_reason="length"))
    with pytest.raises(UpstreamError, match="cortada"):
        make_summarizer(fallback_model=None).summarize("Texto.", "all")


def _worst_case(limits: Limits) -> dict:
    """A maximal answer: every cap reached, every item at its word limit (6-char words like 'abcde ')."""

    def words(n: int) -> str:
        return " ".join(["abcde"] * n)

    return {
        "summary": words(limits.summary_words),
        "notes": [words(NOTE_WORDS)] * limits.notes,
        "tasks": [{"title": words(TASK_WORDS), "due": words(3), "owner": words(3)}] * limits.tasks,
        "topics": [words(TOPIC_WORDS)] * limits.topics,
    }


@pytest.mark.parametrize(
    "limits",
    [*LOCAL_LIMITS.values(), *REMOTE_LIMITS.values()],
    ids=["local-map", "local-reduce", "remote-map", "remote-reduce"],
)
def test_item_caps_fit_the_output_budget(limits):
    answer = json.dumps(_worst_case(limits), ensure_ascii=False, indent=1)
    assert estimate_tokens(answer) <= limits.max_tokens


def test_every_local_prompt_and_its_retry_fit_the_4k_context():
    s = make_summarizer()
    assert s.context_tokens("local-qwen") <= 4096
    biggest_chunk = split_text(sentences(2000), s.map_chunk_tokens)[0]
    for note in (RETRY_JSON, RETRY_SHORTER):
        for kind in ("all", "summary"):
            limits = LOCAL_LIMITS["map"]
            msgs = with_note(map_messages(biggest_chunk, 2, 9, kind, limits), note)
            assert prompt_tokens(msgs) - estimate_tokens(biggest_chunk) <= PROMPT_RESERVE
            assert prompt_tokens(msgs) + limits.max_tokens <= s.context_tokens("local-qwen")
    # reduce: partials that fill the batching budget + instructions + retry note + answer
    partial = _worst_case(LOCAL_LIMITS["map"])
    group = [partial] * 8
    while estimate_tokens(_compact(group)) > s.partials_budget("local-qwen"):
        group.pop()
    assert len(group) >= 2, "two maximal map partials must fit one local reduce"
    msgs = with_note(reduce_messages(group, "all", LOCAL_LIMITS["reduce"]), RETRY_SHORTER)
    assert prompt_tokens(msgs) + LOCAL_LIMITS["reduce"].max_tokens <= s.context_tokens("local-qwen")


def test_batches_never_merge_a_trailing_partial_over_budget():
    part = {"summary": "x" * 1000}  # ~320 tokens each
    budget = estimate_tokens(_compact([part, part])) + 10  # two fit, three don't
    groups = _batches([part] * 5, budget)
    assert [len(g) for g in groups] == [2, 2, 1]
    assert all(estimate_tokens(_compact(g)) <= budget for g in groups)


@respx.mock
def test_trailing_single_partial_is_carried_to_the_next_round_without_a_call():
    route = respx.post(CHAT).mock(return_value=chat_response(json.dumps(GOOD_SUMMARY)))
    s = make_summarizer()
    s.partials_budget = lambda model: estimate_tokens(_compact([GOOD_SUMMARY] * 2)) + 5  # pairs only
    s.summarize("\n\n".join(sentences(30) for _ in range(5)), "all", "local-qwen")
    reduces = [c for c in route.calls if "PARCIAIS" in payload(c)["messages"][1]["content"]]
    n_chunks = len(route.calls) - len(reduces)
    assert n_chunks == len(split_text("\n\n".join(sentences(30) for _ in range(5)), 2500))
    # every reduce prompt holds exactly two partials; the odd one waits for the next round
    for call in reduces:
        assert payload(call)["messages"][1]["content"].split("PARCIAIS:\n", 1)[1].count("\n") == 1
    assert len(reduces) == n_chunks - 1


@respx.mock
def test_prompt_too_big_for_the_local_context_goes_straight_to_the_fallback():
    route = respx.post(CHAT).mock(
        side_effect=lambda req: chat_response(json.dumps(GOOD_SUMMARY), json.loads(req.content)["model"])
    )
    s = make_summarizer()
    big = {**GOOD_SUMMARY, "summary": "resumo parcial " * 400}  # ~1900 tokens: a forced pair overflows 4K
    out = s._call(
        Run("local-qwen", "tier2-cheap"), "reduce", lambda lim: reduce_messages([big, big], "all", lim), "all"
    )
    assert out["tasks"] == GOOD_SUMMARY["tasks"]
    assert [payload(c)["model"] for c in route.calls] == ["tier2-cheap"]
