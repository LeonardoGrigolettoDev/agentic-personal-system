"""Optional speaker diarization (pyannote.audio, extra 'diarize'). Absent by default -> HTTP 501."""

import importlib.util
import logging
import os
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from edge.errors import NotImplementedFeature, Unavailable
from edge.whisper import Segment

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Turn:
    start: float
    end: float
    speaker: str


Diarizer = Callable[[Path], list[Turn]]


def unavailable_reason(hf_token: str | None) -> str | None:
    if importlib.util.find_spec("pyannote") is None or importlib.util.find_spec("pyannote.audio") is None:
        return (
            "Diarização indisponível: o extra opcional 'diarize' (pyannote.audio) não está instalado nesta imagem. "
            "Reconstrua com --build-arg EDGE_EXTRAS=diarize (ou `uv sync --extra diarize`) e defina HF_TOKEN."
        )
    if not hf_token:
        return (
            "Diarização indisponível: defina HF_TOKEN (token do Hugging Face com os termos do modelo pyannote aceitos)."
        )
    return None


def require(hf_token: str | None) -> None:
    reason = unavailable_reason(hf_token)
    if reason:
        raise NotImplementedFeature(reason)


def assign_speakers(segments: Sequence[Segment], turns: Sequence[Turn]) -> list[Segment]:
    """Label each segment with the speaker whose turns overlap it the most (None when nothing overlaps)."""
    out = []
    for seg in segments:
        overlap: dict[str, float] = {}
        for t in turns:
            o = min(seg.end, t.end) - max(seg.start, t.start)
            if o > 0:
                overlap[t.speaker] = overlap.get(t.speaker, 0.0) + o
        out.append(replace(seg, speaker=max(overlap, key=overlap.get) if overlap else None))
    return out


class PyannoteDiarizer:
    """Lazily loads the pipeline once; pyannote 4.x API (`token=`, output.speaker_diarization)."""

    def __init__(self, model: str, hf_token: str) -> None:
        self.model = model
        self.hf_token = hf_token
        self._pipeline = None
        self._lock = threading.Lock()

    def _load(self):
        with self._lock:
            if self._pipeline is None:
                os.environ.setdefault("PYANNOTE_METRICS_ENABLED", "0")
                from pyannote.audio import Pipeline  # optional extra

                log.info("loading diarization pipeline", extra={"model": self.model})
                try:
                    self._pipeline = Pipeline.from_pretrained(self.model, token=self.hf_token)
                except Exception as exc:  # noqa: BLE001 - gated model, bad token, offline hub, unwritable HF_HOME
                    log.error("diarization model load failed", extra={"model": self.model, "error": repr(exc)[:500]})
                    raise Unavailable(
                        f"não foi possível carregar o modelo de diarização {self.model} ({type(exc).__name__}): "
                        "confira HF_TOKEN, se os termos do modelo foram aceitos e se HF_HOME é gravável"
                    ) from None
                if self._pipeline is None:  # pyannote returns None when the hub refuses the download
                    raise Unavailable(f"o Hugging Face recusou o modelo {self.model}: confira HF_TOKEN e os termos")
            return self._pipeline

    def __call__(self, wav: Path) -> list[Turn]:
        output = self._load()(str(wav))
        annotation = getattr(output, "speaker_diarization", output)
        turns = []
        for item in annotation.itertracks(yield_label=True) if hasattr(annotation, "itertracks") else annotation:
            turn, speaker = item[0], item[-1]
            turns.append(Turn(float(turn.start), float(turn.end), str(speaker)))
        return turns
