import subprocess
import wave

import pytest

from edge import media
from edge.errors import Unavailable, Unprocessable, UpstreamTimeout

from .conftest import put_media, write_wav


def test_extract_audio_endpoint(client, settings, ffmpeg):
    uri = put_media(settings, "input/0123456789abcdef0123456789abcdef.mkv")
    resp = client.post("/extract_audio", json={"source": uri})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["uri"] == "storage://media/processing/0123456789abcdef0123456789abcdef.wav"
    assert body["duration_s"] == pytest.approx(12.0)
    assert body["media_duration_s"] == pytest.approx(12.0)
    assert body["sample_rate"] == 16000 and body["channels"] == 1
    out = settings.storage_root / "media/processing/0123456789abcdef0123456789abcdef.wav"
    with wave.open(str(out)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2)

    probe, convert = ffmpeg.calls
    assert probe[0] == "ffprobe" and probe[-1].startswith("file:/")
    assert convert[0] == "ffmpeg"
    for flag, value in (("-ar", "16000"), ("-ac", "1"), ("-c:a", "pcm_s16le"), ("-map", "0:a:0")):
        assert convert[convert.index(flag) + 1] == value
    assert convert[convert.index("-i") + 1].startswith("file:/")
    assert convert[-1].startswith("file:/") and convert[-1].endswith(".wav.part")
    assert not list((settings.storage_root / "media/processing").glob("*.part"))


def test_non_uuid_sources_get_slug_and_hash_ids(client, settings, ffmpeg):
    uri = put_media(settings, "input/Aula de Cálculo.mp4")
    body = client.post("/extract_audio", json={"source": uri}).json()
    assert body["id"].startswith("aula-de-calculo-") and len(body["id"]) == len("aula-de-calculo-") + 12
    assert client.post("/extract_audio", json={"source": uri}).json()["id"] == body["id"]  # same file, same id


def test_a_new_file_at_the_same_path_gets_a_new_id(settings):
    import os

    from edge.pipeline import media_id

    uri = put_media(settings, "input/reuniao.mp4", b"primeira reuniao" * 100)
    path = settings.storage_root / "media/input/reuniao.mp4"
    first, mtime = media_id(uri, path), path.stat().st_mtime_ns
    path.write_bytes(b"segunda reuniao!" * 100)  # same size and (restored) mtime: only the bytes differ
    os.utime(path, ns=(mtime, mtime))
    assert media_id(uri, path) != first
    os.utime(path, ns=(mtime + 1, mtime + 1))
    assert media_id(uri, path) != first
    # our own processing WAVs keep the id they were named with; a user-named WAV there does not
    own = put_media(settings, f"processing/{first}.wav")
    assert media_id(own, settings.storage_root / f"media/processing/{first}.wav") == first
    other = put_media(settings, "processing/gravacao.wav")
    assert media_id(other, settings.storage_root / "media/processing/gravacao.wav").startswith("gravacao-")


def test_media_without_audio_is_422(client, settings, ffmpeg):
    ffmpeg.has_audio = False
    uri = put_media(settings, "input/mute.mp4")
    resp = client.post("/extract_audio", json={"source": uri})
    assert resp.status_code == 422
    assert "áudio" in resp.json()["error"]


def test_ffmpeg_failure_is_422_and_leaves_no_partials(client, settings, ffmpeg):
    ffmpeg.fail_with = "Invalid data found when processing input"
    uri = put_media(settings, "input/broken.mp3")
    resp = client.post("/extract_audio", json={"source": uri})
    assert resp.status_code == 422
    assert "Invalid data" in resp.json()["error"]
    assert not list((settings.storage_root / "media").rglob("*.part"))


@pytest.mark.parametrize(
    "source,status",
    [
        ("storage://media/../etc/passwd", 400),
        ("storage://kb/notes/a.mp3", 400),
        ("storage://media/input/missing.mp3", 404),
        ("/etc/passwd", 422),
    ],
)
def test_source_validation(client, ffmpeg, source, status):
    assert client.post("/extract_audio", json={"source": source}).status_code == status
    assert ffmpeg.calls == []


def test_missing_binary_is_503(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr("edge.media.subprocess.run", boom)
    with pytest.raises(Unavailable):
        media.run(["ffmpeg", "-version"], timeout=1)


def test_timeout_is_504(monkeypatch):
    def slow(args, **k):
        raise subprocess.TimeoutExpired(args, k["timeout"])

    monkeypatch.setattr("edge.media.subprocess.run", slow)
    with pytest.raises(UpstreamTimeout):
        media.run(["ffmpeg"], timeout=1)


def test_probe_rejects_unknown_media(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "edge.media.subprocess.run", lambda args, **k: subprocess.CompletedProcess(args, 1, b"", b"moov atom not found")
    )
    with pytest.raises(Unprocessable, match="moov atom"):
        media.probe(tmp_path / "x.mp4")


def tone_with_pauses(path, seconds, pauses, rate=16000):
    """Loud tone with 0.4 s of silence starting at each second in `pauses`."""
    import math
    import struct

    quiet = {int(p * rate) + i for p in pauses for i in range(int(0.4 * rate))}
    frames = b"".join(
        struct.pack("<h", 0 if i in quiet else int(8000 * math.sin(i / 5))) for i in range(seconds * rate)
    )
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)
    return path


def test_split_wav_cuts_in_pauses_and_merges_short_tail(tmp_path):
    wav = tone_with_pauses(tmp_path / "a.wav", 100, pauses=[25.0, 52.0])
    chunks = media.split_wav(wav, 30, tmp_path / "chunks")
    offsets = [off for _, off in chunks]
    # nominal cuts at 30 s and ~55 s move back into the pauses; the 48 s remainder is one last chunk
    assert offsets[0] == 0.0
    assert 25.0 <= offsets[1] <= 25.4 and 52.0 <= offsets[2] <= 52.4 and len(chunks) == 3
    durations = [media.wav_duration(p) for p, _ in chunks]
    assert sum(durations) == pytest.approx(100.0)
    assert [round(o + d, 4) for o, d in zip(offsets, durations, strict=True)][:-1] == [round(o, 4) for o in offsets[1:]]


def test_split_wav_without_pauses_still_progresses(tmp_path):
    wav = write_wav(tmp_path / "a.wav", 100)
    chunks = media.split_wav(wav, 30, tmp_path / "chunks")
    offsets = [off for _, off in chunks]
    assert len(chunks) == 3 and 20 <= offsets[1] <= 30 and 40 <= offsets[2] <= 60
    assert sum(media.wav_duration(p) for p, _ in chunks) == pytest.approx(100.0)


def test_split_wav_short_or_disabled_returns_original(tmp_path):
    wav = write_wav(tmp_path / "a.wav", 20)
    assert media.split_wav(wav, 600, tmp_path / "c") == [(wav, 0.0)]
    assert media.split_wav(wav, 0, tmp_path / "c") == [(wav, 0.0)]
    assert not (tmp_path / "c").exists()


def test_media_kind():
    assert media.media_kind("x.MP4") == "video"
    assert media.media_kind("x.opus") == "audio"
    assert media.media_kind("x.txt") is None
