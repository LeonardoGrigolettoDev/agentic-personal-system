import json
from itertools import pairwise

import httpx
import pytest
import respx

from edge.subtitles import render_srt, srt_timestamp
from edge.whisper import Segment, parse_verbose_json

from .conftest import SEGMENTS, WHISPER, put_media, whisper_json

UUID = "abcdefabcdefabcdefabcdefabcdef12"


def _fields(request: httpx.Request) -> dict[str, str]:
    """Form fields of a multipart request (the file part is reported by filename)."""
    ctype = request.headers["content-type"]
    boundary = ctype.split("boundary=")[1].encode()
    out = {}
    for part in request.content.split(b"--" + boundary):
        if b"Content-Disposition" not in part:
            continue
        head, _, body = part.partition(b"\r\n\r\n")
        name = head.split(b'name="')[1].split(b'"')[0].decode()
        out[name] = (
            head.split(b'filename="')[1].split(b'"')[0].decode()
            if b"filename=" in head
            else body.rstrip(b"\r\n").decode()
        )
    return out


@respx.mock
def test_transcribe_writes_txt_srt_and_returns_segments(client, settings, ffmpeg):
    route = respx.post(f"{WHISPER}/inference").mock(return_value=httpx.Response(200, json=whisper_json()))
    uri = put_media(settings, f"input/{UUID}.m4a")
    resp = client.post("/transcribe", json={"source": uri})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["text"] == "Bom dia, pessoal.\nJoão vai preparar o backup até sexta.\n"
    assert body["segments"] == [
        {"start": 0.0, "end": 2.5, "text": "Bom dia, pessoal."},
        {"start": 2.5, "end": 6.0, "text": "João vai preparar o backup até sexta."},
    ]
    assert body["txt_uri"] == f"storage://media/transcripts/{UUID}.txt"
    assert body["srt_uri"] == f"storage://media/transcripts/{UUID}.srt"
    assert body["cached"] is False
    srt = (settings.storage_root / f"media/transcripts/{UUID}.srt").read_text()
    assert srt.startswith("1\n00:00:00,000 --> 00:00:02,500\nBom dia, pessoal.\n\n2\n00:00:02,500 --> 00:00:06,000\n")

    fields = _fields(route.calls.last.request)
    assert fields["response_format"] == "verbose_json"
    assert fields["language"] == "pt"
    assert fields["token_timestamps"] == "false"
    assert fields["file"].endswith(".wav")
    # the job removed its own processing WAV; the original stays in input until a pipeline archives it
    assert list((settings.storage_root / "media/processing").iterdir()) == []
    assert (settings.storage_root / f"media/input/{UUID}.m4a").exists()


@respx.mock
def test_long_audio_is_chunked_with_offsets(tmp_path, ffmpeg):
    from .conftest import make_settings, open_client

    settings = make_settings(tmp_path, EDGE_WHISPER_CHUNK_SECONDS="60")
    ffmpeg.duration = 150
    route = respx.post(f"{WHISPER}/inference").mock(return_value=httpx.Response(200, json=whisper_json()))
    uri = put_media(settings, f"input/{UUID}.mp3")
    with open_client(settings) as client:
        body = client.post("/transcribe", json={"source": uri, "language": "en"}).json()
    starts = [s["start"] for s in body["segments"]]
    offsets = starts[::2]  # each chunk returns the same two segments, shifted by the chunk offset
    assert route.call_count == len(offsets) >= 2
    assert offsets[0] == 0.0 and all(50 <= b - a <= 60 for a, b in pairwise(offsets))
    assert starts[1::2] == pytest.approx([o + 2.5 for o in offsets])
    assert body["duration_s"] == 150.0
    assert list((settings.storage_root / "media/processing").iterdir()) == []


@respx.mock
def test_transcript_is_reused_unless_forced(client, settings, ffmpeg):
    route = respx.post(f"{WHISPER}/inference").mock(return_value=httpx.Response(200, json=whisper_json()))
    uri = put_media(settings, f"input/{UUID}.ogg")
    client.post("/transcribe", json={"source": uri})
    second = client.post("/transcribe", json={"source": uri}).json()
    assert second["cached"] is True and route.call_count == 1
    other_language = client.post("/transcribe", json={"source": uri, "language": "en"}).json()
    assert other_language["cached"] is False and route.call_count == 2
    client.post("/transcribe", json={"source": uri, "language": "en", "force": True})
    assert route.call_count == 3


@respx.mock
def test_processing_wav_source_is_used_directly_and_kept(client, settings, ffmpeg):
    respx.post(f"{WHISPER}/inference").mock(return_value=httpx.Response(200, json=whisper_json()))
    extracted = client.post("/extract_audio", json={"source": put_media(settings, f"input/{UUID}.mp4")}).json()
    calls = len(ffmpeg.calls)
    body = client.post("/transcribe", json={"source": extracted["uri"]}).json()
    assert body["id"] == UUID
    assert len(ffmpeg.calls) == calls  # no second extraction
    assert (settings.storage_root / f"media/processing/{UUID}.wav").exists()


@respx.mock
def test_whisper_errors_map_to_gateway_statuses(client, settings, ffmpeg):
    uri = put_media(settings, f"input/{UUID}.wav")
    respx.post(f"{WHISPER}/inference").mock(return_value=httpx.Response(500, json={"error": "failed to process audio"}))
    resp = client.post("/transcribe", json={"source": uri})
    assert resp.status_code == 502 and "failed to process audio" in resp.json()["error"]

    respx.post(f"{WHISPER}/inference").mock(side_effect=httpx.ConnectError("refused"))
    assert client.post("/transcribe", json={"source": uri}).status_code == 503

    respx.post(f"{WHISPER}/inference").mock(side_effect=httpx.ReadTimeout("slow"))
    assert client.post("/transcribe", json={"source": uri}).status_code == 504

    respx.post(f"{WHISPER}/inference").mock(return_value=httpx.Response(200, text="plain text"))
    assert client.post("/transcribe", json={"source": uri}).status_code == 502
    assert list((settings.storage_root / "media/processing").iterdir()) == []


def test_diarize_without_extra_is_501(client, settings, ffmpeg):
    uri = put_media(settings, f"input/{UUID}.wav")
    resp = client.post("/transcribe", json={"source": uri, "diarize": True})
    assert resp.status_code == 501
    assert "diarize" in resp.json()["error"] and "HF_TOKEN" in resp.json()["error"]
    assert ffmpeg.calls == []


def test_assign_speakers_by_overlap():
    from edge.diarize import Turn, assign_speakers

    segs = [Segment(0, 2, "a"), Segment(2, 5, "b"), Segment(10, 11, "c")]
    turns = [Turn(0, 2.2, "SPEAKER_00"), Turn(2.2, 6, "SPEAKER_01")]
    assert [s.speaker for s in assign_speakers(segs, turns)] == ["SPEAKER_00", "SPEAKER_01", None]


def test_parse_verbose_json_variants():
    assert parse_verbose_json(whisper_json()) == [
        Segment(0.0, 2.5, "Bom dia, pessoal."),
        Segment(2.5, 6.0, "João vai preparar o backup até sexta."),
    ]
    # stereo --diarize output carries a speaker id; blank segments are dropped
    diarized = whisper_json([{**SEGMENTS[0], "speaker": "0"}, {"text": "  ", "start": 3, "end": 4}])
    assert parse_verbose_json(diarized) == [Segment(0.0, 2.5, "Bom dia, pessoal.", "0")]
    assert parse_verbose_json({"text": " só texto ", "duration": 3, "segments": []}) == [Segment(0.0, 3.0, "só texto")]


def test_srt_rendering():
    assert srt_timestamp(3725.5) == "01:02:05,500"
    srt = render_srt([Segment(0, 1.2345, "oi", "SPEAKER_00")])
    assert srt == "1\n00:00:00,000 --> 00:00:01,234\n[SPEAKER_00] oi\n"
    assert json.dumps(Segment(0, 1, "x").to_dict()) == '{"start": 0, "end": 1, "text": "x"}'


@respx.mock
def test_transcribe_never_touches_the_extract_audio_output(client, settings, ffmpeg):
    """/extract_audio hands out processing/<id>.wav; a later job on the same source uses its own scratch."""
    respx.post(f"{WHISPER}/inference").mock(return_value=httpx.Response(200, json=whisper_json()))
    uri = put_media(settings, f"input/{UUID}.mp4")
    extracted = client.post("/extract_audio", json={"source": uri}).json()
    wav = settings.storage_root / extracted["uri"].removeprefix("storage://")
    before = wav.stat().st_mtime_ns
    for body in ({"source": uri}, {"source": uri, "force": True}):
        assert client.post("/transcribe", json=body).status_code == 200
    assert wav.exists() and wav.stat().st_mtime_ns == before
    assert sorted(p.name for p in (settings.storage_root / "media/processing").iterdir()) == [wav.name]
    scratch = ffmpeg.calls[-1][-1].removeprefix("file:")  # the job's own extraction target
    assert scratch.startswith(str(settings.storage_root / f"media/processing/{UUID}.")) and scratch != str(wav)


def test_concurrent_jobs_on_one_source_get_distinct_scratch(settings, ffmpeg, monkeypatch):
    """EDGE_JOB_WORKERS>1: two transcriptions of the same file must not share (and delete) one WAV."""
    import threading

    from edge.pipeline import Edge
    from edge.whisper import Segment

    edge = Edge.from_settings(settings)
    seen, barrier = [], threading.Barrier(2, timeout=5)

    def fake_whisper(chunk, language):
        seen.append(chunk)
        barrier.wait()  # both jobs hold their WAV at the same time
        assert chunk.exists()
        return [Segment(0.0, 1.0, "oi")]

    monkeypatch.setattr(edge.whisper, "transcribe", fake_whisper)
    uri = put_media(settings, f"input/{UUID}.ogg")
    results = []
    threads = [threading.Thread(target=lambda: results.append(edge.transcribe(uri, force=True))) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    edge.close()
    assert len(results) == 2 and len(set(seen)) == 2
    assert list((settings.storage_root / "media/processing").iterdir()) == []
