"""Runs the real ffmpeg/ffprobe when they are installed (skipped otherwise)."""

import shutil
import subprocess
import wave

import pytest

from edge import media

pytestmark = [
    pytest.mark.ffmpeg,
    pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg not installed"),
]


def test_real_extract_audio(tmp_path):
    src = tmp_path / "tone.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=3",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x64:d=3",
            "-shortest",
            "-ac",
            "2",
            "-ar",
            "44100",
            str(src),
        ],
        check=True,
        timeout=60,
    )
    info = media.probe(src)
    assert info.has_audio and info.has_video and info.duration_s == pytest.approx(3, abs=0.2)
    out = tmp_path / "out.wav"
    media.extract_audio(src, out, timeout=60)
    with wave.open(str(out)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2)
    assert media.wav_duration(out) == pytest.approx(3, abs=0.2)
