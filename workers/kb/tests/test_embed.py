import json

import httpx
import pytest

from kb.embed import QUERY_INSTRUCTION, Embedder, EmbeddingError


def make(handler, **kw):
    return Embedder("http://litellm.test", "embed-local", 3, api_key="sk-test", backoff=0,
                    transport=httpx.MockTransport(handler), **kw)


def ok(request: httpx.Request) -> httpx.Response:
    inputs = json.loads(request.content)["input"]
    data = [{"index": i, "embedding": [float(len(t)), 0.0, 1.0]} for i, t in enumerate(inputs)]
    return httpx.Response(200, json={"data": list(reversed(data))})  # out of order on purpose


def test_batches_and_order_and_auth():
    calls = []

    def handler(request):
        calls.append(request)
        return ok(request)

    vectors = make(handler, batch_size=2).embed_documents(["a", "bb", "ccc"])
    assert vectors == [[1.0, 0.0, 1.0], [2.0, 0.0, 1.0], [3.0, 0.0, 1.0]]
    assert [json.loads(c.content)["input"] for c in calls] == [["a", "bb"], ["ccc"]]
    assert calls[0].url.path == "/v1/embeddings"
    assert calls[0].headers["authorization"] == "Bearer sk-test"
    assert json.loads(calls[0].content)["model"] == "embed-local"


def test_query_gets_instruction_prefix_documents_do_not():
    seen = []

    def handler(request):
        seen.extend(json.loads(request.content)["input"])
        return ok(request)

    e = make(handler)
    e.embed_query("o que é RRF?")
    e.embed_documents(["texto"])
    assert seen[0] == QUERY_INSTRUCTION + "o que é RRF?"
    assert seen[0].startswith("Instruct: Given a search query, retrieve relevant passages that answer the query\nQuery: ")
    assert seen[1] == "texto"


def test_dimension_mismatch_raises():
    def handler(request):
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.0] * 768}]})

    with pytest.raises(EmbeddingError, match="768 dims"):
        make(handler).embed_documents(["x"])


def test_retries_transient_errors_then_succeeds():
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.ConnectError("refused")
        if attempts["n"] == 2:
            return httpx.Response(503)
        return ok(request)

    assert make(handler).embed_documents(["x"]) == [[1.0, 0.0, 1.0]]
    assert attempts["n"] == 3


def test_gives_up_after_max_retries_and_fails_fast_on_4xx():
    with pytest.raises(EmbeddingError, match="after 3 attempts"):
        make(lambda r: httpx.Response(502), max_retries=2).embed_documents(["x"])
    calls = {"n": 0}

    def unauthorized(request):
        calls["n"] += 1
        return httpx.Response(401, text="bad key")

    with pytest.raises(EmbeddingError, match="401"):
        make(unauthorized).embed_documents(["x"])
    assert calls["n"] == 1
