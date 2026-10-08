"""whisper.cpp `whisper-server` client (examples/server in ggml-org/whisper.cpp).

POST /inference is multipart: `file` (the audio) plus optional form fields. With
response_format=verbose_json the reply is
  {"task", "language", "duration", "text", "segments": [{"id", "text", "start", "end", "tokens", "words",
   "temperature", "avg_logprob", "no_speech_prob", "speaker"?}], "detected_language"?...}
where start/end are seconds. verbose_json turns token timestamps on by default, which also wraps
segments at 60 characters mid-word, so we send token_timestamps=false to keep whisper's natural
segments; no_language_probabilities=true skips an extra language-detection pass.
"""

import json
import logging
from dataclasses import dataclass, replace
from pathlib import Path

import httpx

from edge.errors import Unavailable, UpstreamError, UpstreamTimeout

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str
    speaker: str | None = None

    def shifted(self, offset: float) -> "Segment":
        return replace(self, start=self.start + offset, end=self.end + offset)

    def to_dict(self) -> dict:
        out = {"start": round(self.start, 3), "end": round(self.end, 3), "text": self.text}
        if self.speaker:
            out["speaker"] = self.speaker
        return out

    @classmethod
    def from_dict(cls, d: dict) -> "Segment":
        return cls(float(d["start"]), float(d["end"]), str(d["text"]), d.get("speaker") or None)


def parse_verbose_json(body: dict) -> list[Segment]:
    segments: list[Segment] = []
    for raw in body.get("segments") or []:
        text = str(raw.get("text") or "").strip()
        if not text:
            continue
        start = float(raw.get("start") or 0.0)
        end = float(raw.get("end") if raw.get("end") is not None else start)
        speaker = raw.get("speaker")
        segments.append(Segment(start, max(start, end), text, str(speaker) if speaker not in (None, "") else None))
    if not segments and str(body.get("text") or "").strip():
        segments.append(Segment(0.0, float(body.get("duration") or 0.0), str(body["text"]).strip()))
    return segments


class WhisperClient:
    def __init__(self, base_url: str, timeout: float, *, client: httpx.Client | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client = client or httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0))

    def close(self) -> None:
        self._client.close()

    def transcribe(self, wav: Path, language: str) -> list[Segment]:
        data = {
            "response_format": "verbose_json",
            "language": language,
            "token_timestamps": "false",
            "no_language_probabilities": "true",
        }
        url = f"{self.base_url}/inference"
        try:
            with wav.open("rb") as fh:
                resp = self._client.post(url, data=data, files={"file": (wav.name, fh, "audio/wav")})
        except httpx.TimeoutException:
            raise UpstreamTimeout(f"whisper não respondeu em {self.timeout:.0f}s") from None
        except httpx.TransportError as exc:
            raise Unavailable(f"whisper indisponível em {self.base_url}: {type(exc).__name__}") from None
        if resp.status_code >= 400:
            raise UpstreamError(f"whisper respondeu HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            body = resp.json()
        except json.JSONDecodeError:
            raise UpstreamError("whisper devolveu uma resposta que não é JSON (versão sem verbose_json?)") from None
        if not isinstance(body, dict):
            raise UpstreamError("whisper devolveu um JSON inesperado")
        if "error" in body and "segments" not in body:
            raise UpstreamError(f"whisper: {str(body['error'])[:300]}")
        return parse_verbose_json(body)

    def healthy(self) -> bool:
        try:
            return self._client.get(f"{self.base_url}/health", timeout=3.0).status_code == 200
        except httpx.HTTPError:
            return False
