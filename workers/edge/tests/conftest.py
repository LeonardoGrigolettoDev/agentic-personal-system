import json
import math
import struct
import subprocess
import wave
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from edge.app import create_app
from edge.config import Settings

KEY = "test-edge-key"
WHISPER = "http://whisper.test"
LITELLM = "http://litellm.test"
KNOWLEDGE = "http://knowledge.test"


def make_settings(tmp_path: Path, **overrides: str) -> Settings:
    env = {
        "EDGE_API_KEY": KEY,
        "STORAGE_LOCAL_ROOT": str(tmp_path / "storage"),
        "WHISPER_URL": WHISPER,
        "LITELLM_BASE_URL": LITELLM,
        "EDGE_LITELLM_KEY": "litellm-edge-key",
        "KNOWLEDGE_URL": KNOWLEDGE,
        "KNOWLEDGE_API_KEY": "knowledge-key",
        "EDGE_LOG_LEVEL": "WARNING",
        "EDGE_MAINTAIN_INTERVAL_HOURS": "0",  # tests that want the schedule turn it on explicitly
    }
    env.update(overrides)
    return Settings.from_env(env)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return make_settings(tmp_path)


def open_client(settings: Settings) -> TestClient:
    client = TestClient(create_app(settings))
    client.headers["Authorization"] = f"Bearer {KEY}"
    return client


@pytest.fixture
def client(settings):
    with open_client(settings) as c:
        yield c


def write_wav(path: Path, seconds: float, rate: int = 16000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(seconds * rate)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        block = b"".join(struct.pack("<h", int(3000 * math.sin(i / 8))) for i in range(rate))
        full, rest = divmod(frames, rate)
        for _ in range(full):
            w.writeframes(block)
        w.writeframes(block[: rest * 2])
    return path


def put_media(settings: Settings, key: str, data: bytes = b"\x00fake media bytes\x00") -> str:
    path = settings.storage_root / "media" / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return f"storage://media/{key}"


class FakeFFmpeg:
    """Stands in for subprocess.run: answers ffprobe with JSON, makes ffmpeg write a real WAV."""

    def __init__(self, duration: float = 12.0, has_audio: bool = True, has_video: bool = True) -> None:
        self.duration = duration
        self.has_audio = has_audio
        self.has_video = has_video
        self.fail_with: str | None = None
        self.calls: list[list[str]] = []

    def __call__(self, args, **kwargs):
        assert isinstance(args, list), "commands must be argument lists"
        assert not kwargs.get("shell"), "never use a shell"
        self.calls.append(args)
        if args[0] == "ffprobe":
            streams = [{"codec_type": "audio"}] * self.has_audio + [{"codec_type": "video"}] * self.has_video
            out = {"streams": streams, "format": {"duration": f"{self.duration:.6f}"}}
            return subprocess.CompletedProcess(args, 0, json.dumps(out).encode(), b"")
        if args[0] == "ffmpeg":
            if self.fail_with:
                return subprocess.CompletedProcess(args, 1, b"", self.fail_with.encode())
            write_wav(Path(args[-1].removeprefix("file:")), self.duration)
            return subprocess.CompletedProcess(args, 0, b"", b"")
        raise AssertionError(f"unexpected command {args}")


@pytest.fixture
def ffmpeg(monkeypatch) -> FakeFFmpeg:
    fake = FakeFFmpeg()
    monkeypatch.setattr("edge.media.subprocess.run", fake)
    return fake


SEGMENTS = [
    {"id": 0, "text": " Bom dia, pessoal.", "start": 0.0, "end": 2.5, "tokens": [1, 2], "avg_logprob": -0.2},
    {"id": 1, "text": " João vai preparar o backup até sexta.", "start": 2.5, "end": 6.0, "tokens": [3]},
]


def whisper_json(segments=None, duration: float = 6.0) -> dict:
    segments = SEGMENTS if segments is None else segments
    return {
        "task": "transcribe",
        "language": "portuguese",
        "duration": duration,
        "text": "".join(s["text"] + "\n" for s in segments),
        "segments": segments,
    }


def chat_response(content: str, model: str = "local-qwen", finish_reason: str = "stop") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "model": model,
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": finish_reason}
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        },
    )


GOOD_SUMMARY = {
    "summary": "A equipe combinou o backup do banco.",
    "notes": ["Backup antes da migração"],
    "tasks": [{"title": "Preparar o backup", "due": "sexta", "owner": "João"}],
    "topics": ["backup", "banco de dados"],
}
