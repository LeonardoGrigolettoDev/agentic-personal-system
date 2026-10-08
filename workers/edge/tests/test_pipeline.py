import json

import httpx
import respx

from edge.storage import LocalStorage

from .conftest import GOOD_SUMMARY, KNOWLEDGE, LITELLM, WHISPER, chat_response, put_media, whisper_json

UUID = "0123456789abcdef0123456789abcdef"


def mock_backends(knowledge_status: int = 200):
    respx.post(f"{WHISPER}/inference").mock(return_value=httpx.Response(200, json=whisper_json()))
    respx.post(f"{LITELLM}/v1/chat/completions").mock(return_value=chat_response(json.dumps(GOOD_SUMMARY)))
    return respx.post(f"{KNOWLEDGE}/v1/ingest").mock(
        side_effect=lambda req: httpx.Response(
            knowledge_status,
            json={"action": "created", "source_uri": json.loads(req.content)["source_uri"]}
            if knowledge_status == 200
            else {"detail": "boom"},
        )
    )


@respx.mock
def test_pipeline_full_chain(client, settings, ffmpeg):
    ingest = mock_backends()
    uri = put_media(settings, f"input/{UUID}.mp4")
    resp = client.post(
        "/pipeline", json={"source": uri, "tenant": "pessoal", "domain": "learning", "title": "Aula 3 de Cálculo"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["media_kind"] == "video" and body["segments"] == 2
    assert body["summary"]["tasks"] == GOOD_SUMMARY["tasks"]
    assert body["archived_uri"] == f"storage://media/archive/{UUID}.mp4"
    assert body["summary_uri"] == f"storage://media/transcripts/{UUID}.summary.md"

    docs = [json.loads(c.request.content) for c in ingest.calls]
    assert [d["source_uri"] for d in docs] == [
        f"storage://media/transcripts/{UUID}.txt",
        f"storage://media/transcripts/{UUID}.summary.md",
    ]
    for d in docs:
        assert d["tenant"] == "pessoal" and d["domain"] == "learning" and d["source_type"] == "video"
        assert d["metadata"]["media_uri"] == body["archived_uri"]
    assert docs[0]["title"] == "Transcrição: Aula 3 de Cálculo"
    assert docs[0]["content"].startswith("Bom dia, pessoal.")
    assert docs[1]["content"].startswith("# Resumo: Aula 3 de Cálculo")
    assert ingest.calls.last.request.headers["authorization"] == "Bearer knowledge-key"
    assert [i["action"] for i in body["ingest"]] == ["created", "created"]

    media = settings.storage_root / "media"
    assert not (media / "input" / f"{UUID}.mp4").exists()
    assert (media / "archive" / f"{UUID}.mp4").exists()
    assert list((media / "processing").iterdir()) == []
    for suffix in (".txt", ".srt", ".json", ".summary.md", ".summary.json"):
        assert (media / "transcripts" / f"{UUID}{suffix}").exists()


@respx.mock
def test_ingest_failure_keeps_original_and_retry_reuses_transcript(client, settings, ffmpeg):
    ingest = mock_backends(knowledge_status=500)
    uri = put_media(settings, f"input/{UUID}.m4a")
    resp = client.post("/pipeline", json={"source": uri, "tenant": "nitro"})
    assert resp.status_code == 502 and "knowledge" in resp.json()["error"]
    assert (settings.storage_root / f"media/input/{UUID}.m4a").exists()

    ingest.mock(side_effect=lambda req: httpx.Response(200, json={"action": "created", "source_uri": "x"}))
    whisper_calls = respx.routes[0].call_count
    body = client.post("/pipeline", json={"source": uri, "tenant": "nitro"}).json()
    assert body["transcript_cached"] is True and respx.routes[0].call_count == whisper_calls
    assert body["media_kind"] == "audio"
    assert json.loads(ingest.calls.last.request.content)["source_type"] == "audio"


@respx.mock
def test_ingest_false_skips_knowledge(client, settings, ffmpeg):
    ingest = mock_backends()
    uri = put_media(settings, f"input/{UUID}.opus")
    body = client.post("/pipeline", json={"source": uri, "tenant": "shared", "ingest": False}).json()
    assert ingest.call_count == 0 and body["ingest"] == []
    assert body["archived_uri"] is not None


@respx.mock
def test_silence_is_not_ingested(client, settings, ffmpeg):
    ingest = mock_backends()
    respx.post(f"{WHISPER}/inference").mock(return_value=httpx.Response(200, json=whisper_json([])))
    uri = put_media(settings, f"input/{UUID}.wav")
    body = client.post("/pipeline", json={"source": uri, "tenant": "pessoal"}).json()
    assert ingest.call_count == 0 and body["warnings"]


def test_invalid_tenant_or_domain_fails_before_any_work(client, settings, ffmpeg):
    uri = put_media(settings, f"input/{UUID}.mp4")
    assert client.post("/pipeline", json={"source": uri, "tenant": "work"}).status_code == 400
    assert client.post("/pipeline", json={"source": uri, "tenant": "nitro", "domain": "x"}).status_code == 400
    assert ffmpeg.calls == []


@respx.mock
def test_process_video_returns_transcript_and_summary(client, settings, ffmpeg):
    mock_backends()
    uri = put_media(settings, f"input/{UUID}.mov")
    body = client.post("/process_video", json={"source": uri, "kind": "tasks"}).json()
    assert body["transcript"]["segments"][1]["text"].startswith("João")
    assert body["summary"]["tasks"][0]["owner"] == "João"
    assert body["summary_uri"].endswith(".summary.md")
    assert (settings.storage_root / f"media/input/{UUID}.mov").exists()  # only /pipeline archives


@respx.mock
def test_embed(client):
    route = respx.post(f"{LITELLM}/v1/embeddings").mock(
        side_effect=lambda req: httpx.Response(
            200,
            json={
                "data": [
                    {"index": i, "embedding": [float(i)] * 4}
                    for i in reversed(range(len(json.loads(req.content)["input"])))
                ]
            },
        )
    )
    body = client.post("/embed", json={"texts": ["a", "b", "c"]}).json()
    assert body == {"model": "embed-local", "dim": 4, "vectors": [[0.0] * 4, [1.0] * 4, [2.0] * 4]}
    assert json.loads(route.calls.last.request.content)["model"] == "embed-local"
    assert client.post("/embed", json={"texts": []}).status_code == 422
    assert client.post("/embed", json={"texts": [" "]}).status_code == 422

    route.mock(return_value=httpx.Response(401, json={"error": "invalid key"}))
    assert client.post("/embed", json={"texts": ["a"]}).status_code == 502
    route.mock(side_effect=httpx.ConnectError("down"))
    assert client.post("/embed", json={"texts": ["a"]}).status_code == 503


@respx.mock
def test_a_second_recording_at_the_same_path_never_replaces_the_first(client, settings, ffmpeg):
    """Phone exports and `aula05.m4a` reuse names: each version gets its own id, KB documents and archive."""
    ingest = mock_backends()
    bodies = []
    for content in (b"primeira reuniao" * 64, b"segunda reuniao, outra pauta" * 64):
        uri = put_media(settings, "input/reuniao.mp4", content)
        resp = client.post("/pipeline", json={"source": uri, "tenant": "nitro"})
        assert resp.status_code == 200, resp.text
        bodies.append(resp.json())
    first, second = bodies
    assert first["id"] != second["id"] and first["id"].startswith("reuniao-")
    uris = [json.loads(c.request.content)["source_uri"] for c in ingest.calls]
    assert len(uris) == 4 and len(set(uris)) == 4  # two KB documents per recording, none overwritten
    assert first["archived_uri"] == "storage://media/archive/reuniao.mp4"
    assert second["archived_uri"] == "storage://media/archive/reuniao-1.mp4"
    archive = settings.storage_root / "media/archive"
    assert (archive / "reuniao.mp4").read_bytes() == b"primeira reuniao" * 64
    assert (archive / "reuniao-1.mp4").read_bytes() == b"segunda reuniao, outra pauta" * 64
    for body in bodies:
        assert (settings.storage_root / body["txt_uri"].removeprefix("storage://")).exists()
    second_meta = json.loads(ingest.calls[-1].request.content)["metadata"]
    assert second_meta["media_uri"] == second["archived_uri"]  # the name reserved before ingest is the real one


@respx.mock
def test_source_archived_by_a_concurrent_job_is_a_warning_not_a_crash(client, settings, ffmpeg, monkeypatch):
    mock_backends()
    uri = put_media(settings, f"input/{UUID}.mp4")
    original = LocalStorage.move

    def racing_move(self, src_uri, dst_uri):
        self.path(src_uri).unlink()  # the other job got there first
        return original(self, src_uri, dst_uri)

    monkeypatch.setattr(LocalStorage, "move", racing_move)
    body = client.post("/pipeline", json={"source": uri, "tenant": "nitro"}).json()
    assert body["archived_uri"] is None and any("outro job" in w for w in body["warnings"])
