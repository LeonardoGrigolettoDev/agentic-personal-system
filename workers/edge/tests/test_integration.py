"""Optional checks against live local services; each test is skipped when its service is not reachable.

EDGE_IT_LLM_URL        fake/real OpenAI-compatible chat endpoint (default http://127.0.0.1:14100/v1)
EDGE_IT_KNOWLEDGE_URL  knowledge service (default http://127.0.0.1:18092), key EDGE_IT_KNOWLEDGE_KEY (default kk)
EDGE_IT_WHISPER_URL    real whisper-server (default http://127.0.0.1:8178, the compose host port);
EDGE_IT_WHISPER_SAMPLE optional 16 kHz mono speech WAV (e.g. whisper.cpp samples/jfk.wav, language en)
ffmpeg is stubbed (the WAV is used directly as a processing file).
"""

import json
import os
import shutil
import socket
import wave
from urllib.parse import urlsplit

import httpx
import pytest
import respx

from .conftest import (
    GOOD_SUMMARY,
    WHISPER,
    chat_response,
    make_settings,
    open_client,
    put_media,
    whisper_json,
    write_wav,
)

pytestmark = pytest.mark.integration

LLM_URL = os.environ.get("EDGE_IT_LLM_URL", "http://127.0.0.1:14100/v1")
KNOWLEDGE_URL = os.environ.get("EDGE_IT_KNOWLEDGE_URL", "http://127.0.0.1:18092")
KNOWLEDGE_KEY = os.environ.get("EDGE_IT_KNOWLEDGE_KEY", "kk")
WHISPER_URL = os.environ.get("EDGE_IT_WHISPER_URL", "http://127.0.0.1:8178")
WHISPER_SAMPLE = os.environ.get("EDGE_IT_WHISPER_SAMPLE")
# Fixed id: re-runs update the same two documents instead of piling up new ones.
UUID = "edee0000000000000000000000000001"


def reachable(url: str) -> bool:
    if os.environ.get("EDGE_SKIP_INTEGRATION"):
        return False
    parts = urlsplit(url)
    try:
        with socket.create_connection((parts.hostname, parts.port or 80), timeout=0.5):
            return True
    except OSError:
        return False


@pytest.mark.skipif(not reachable(LLM_URL), reason=f"no LLM at {LLM_URL}")
def test_summarize_against_live_llm(tmp_path):
    """The local fake answers plain prose: edge must retry once per model, then try the fallback, then 502."""
    settings = make_settings(tmp_path, LITELLM_BASE_URL=LLM_URL)
    with open_client(settings) as client:
        resp = client.post("/summarize", json={"text": "João vai preparar o backup na sexta.", "kind": "tasks"})
    if resp.status_code == 200:  # a real model answered with JSON
        assert set(resp.json()) >= {"summary", "notes", "tasks", "topics", "meta"}
    else:
        assert resp.status_code == 502
        assert "local-qwen, tier2-cheap" in resp.json()["error"]


@pytest.mark.skipif(not reachable(KNOWLEDGE_URL), reason=f"no knowledge service at {KNOWLEDGE_URL}")
def test_pipeline_ingests_into_live_knowledge(tmp_path, ffmpeg):
    settings = make_settings(tmp_path, KNOWLEDGE_URL=KNOWLEDGE_URL, KNOWLEDGE_API_KEY=KNOWLEDGE_KEY)
    uri = put_media(settings, f"input/{UUID}.m4a")
    with respx.mock(assert_all_called=False) as mock:
        mock.route(host=urlsplit(KNOWLEDGE_URL).hostname, port=urlsplit(KNOWLEDGE_URL).port).pass_through()
        mock.post(f"{WHISPER}/inference").mock(return_value=httpx.Response(200, json=whisper_json()))
        mock.post(f"{settings.litellm_base_url}/v1/chat/completions").mock(
            return_value=chat_response(json.dumps(GOOD_SUMMARY))
        )
        with open_client(settings) as client:
            resp = client.post(
                "/pipeline",
                json={"source": uri, "tenant": "pessoal", "domain": "learning", "title": "Teste de integração edge"},
            )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [d["document"] for d in body["ingest"]] == ["transcript", "summary"]
    assert all(d["action"] in ("new", "updated", "metadata", "unchanged") for d in body["ingest"])  # kb ingest actions
    assert {d["source_uri"] for d in body["ingest"]} == {
        f"storage://media/transcripts/{UUID}.txt",
        f"storage://media/transcripts/{UUID}.summary.md",
    }

    found = httpx.post(
        f"{KNOWLEDGE_URL}/v1/search",
        headers={"Authorization": f"Bearer {KNOWLEDGE_KEY}"},
        json={"tenant": "pessoal", "query": "preparar o backup", "domain": "learning", "k": 20},
        timeout=30,
    ).json()
    uris = {r.get("source_uri") for r in found["results"]}
    assert f"storage://media/transcripts/{UUID}.txt" in uris or f"storage://media/transcripts/{UUID}.summary.md" in uris


def _repeat_wav(src: str, dst, times: int) -> None:
    with wave.open(src) as r:
        params, frames = r.getparams(), r.readframes(r.getnframes())
    dst.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(dst), "wb") as w:
        w.setparams(params)
        for _ in range(times):
            w.writeframes(frames)


@pytest.mark.skipif(not reachable(WHISPER_URL), reason=f"no whisper-server at {WHISPER_URL}")
def test_transcribe_against_live_whisper(tmp_path):
    """Real whisper-server: verbose_json parsing, chunk offsets and the .txt/.srt outputs."""
    settings = make_settings(tmp_path, WHISPER_URL=WHISPER_URL, EDGE_WHISPER_CHUNK_SECONDS="30")
    wav = settings.storage_root / "media/processing/live-whisper-test.wav"
    if WHISPER_SAMPLE:
        _repeat_wav(WHISPER_SAMPLE, wav, 7)  # ~77 s -> chunks at 0 s and 30 s (+47 s tail)
    else:
        write_wav(wav, 70)
    language = os.environ.get("EDGE_IT_WHISPER_LANGUAGE", "en" if WHISPER_SAMPLE else "pt")
    with open_client(settings) as client:
        resp = client.post(
            "/transcribe", json={"source": "storage://media/processing/live-whisper-test.wav", "language": language}
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["duration_s"] >= 70
    assert all(s["end"] >= s["start"] for s in body["segments"])
    srt = (settings.storage_root / body["srt_uri"].removeprefix("storage://")).read_text()
    assert srt.count(" --> ") == len(body["segments"])
    if WHISPER_SAMPLE:
        assert body["text"].strip()
        assert any(s["start"] >= 30 for s in body["segments"]), "second chunk must be offset by 30 s"
    assert wav.exists()  # caller-provided processing WAVs are not deleted by transcribe
    shutil.rmtree(settings.storage_root, ignore_errors=True)
