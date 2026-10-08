"""ffmpeg/ffprobe wrappers (argument lists, never a shell) and WAV chunking for whisper."""

import json
import logging
import subprocess
import sys
import wave
from array import array
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from edge.errors import Unavailable, Unprocessable, UpstreamTimeout

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
AUDIO_EXTS = frozenset(
    {"aac", "aif", "aiff", "amr", "caf", "flac", "m4a", "mp3", "oga", "ogg", "opus", "wav", "weba", "wma"}
)
VIDEO_EXTS = frozenset(
    {"3g2", "3gp", "avi", "flv", "m2ts", "m4v", "mkv", "mov", "mp4", "mpeg", "mpg", "mts", "ogv", "ts", "webm", "wmv"}
)
ALLOWED_EXTS = AUDIO_EXTS | VIDEO_EXTS
PROBE_TIMEOUT = 60.0
# A last chunk shorter than this is merged into the previous one (whisper does poorly on tiny clips).
MIN_TAIL_SECONDS = 30
SILENCE_SEARCH_SECONDS = 10


@dataclass(frozen=True)
class Probe:
    duration_s: float | None
    has_audio: bool
    has_video: bool


def extension(name: str) -> str:
    return PurePosixPath(name).suffix.lower().lstrip(".")


def media_kind(name: str) -> str | None:
    """'audio' | 'video' by file extension; None when the extension is not a supported media type."""
    ext = extension(name)
    if ext in VIDEO_EXTS:
        return "video"
    if ext in AUDIO_EXTS:
        return "audio"
    return None


def _tail(stderr: bytes, limit: int = 400) -> str:
    return stderr.decode("utf-8", errors="replace").strip()[-limit:]


def run(args: list[str], timeout: float) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, capture_output=True, timeout=timeout, check=False, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        raise Unavailable(f"{args[0]} não está instalado neste host") from None
    except subprocess.TimeoutExpired:
        raise UpstreamTimeout(f"{args[0]} excedeu o limite de {timeout:.0f}s") from None


def probe(path: Path, timeout: float = PROBE_TIMEOUT) -> Probe:
    # `file:` stops ffmpeg from interpreting the name as another protocol (concat:, http:, pipe:...).
    args = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_entries",
        "format=duration:stream=codec_type",
        f"file:{path}",
    ]
    res = run(args, timeout)
    if res.returncode != 0:
        raise Unprocessable(f"ffprobe não reconheceu a mídia: {_tail(res.stderr)}")
    try:
        info = json.loads(res.stdout or b"{}")
    except json.JSONDecodeError:
        raise Unprocessable("ffprobe devolveu uma saída inválida") from None
    kinds = {s.get("codec_type") for s in info.get("streams") or []}
    try:
        duration = float((info.get("format") or {}).get("duration"))
    except (TypeError, ValueError):
        duration = None
    return Probe(duration_s=duration, has_audio="audio" in kinds, has_video="video" in kinds)


def extract_audio(src: Path, dst: Path, timeout: float) -> None:
    """First audio stream -> 16 kHz mono PCM s16le WAV at `dst` (written atomically)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".part")
    args = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        f"file:{src}",
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-map_metadata",
        "-1",
        "-ac",
        "1",
        "-ar",
        str(SAMPLE_RATE),
        "-c:a",
        "pcm_s16le",
        "-f",
        "wav",
        f"file:{tmp}",
    ]
    try:
        res = run(args, timeout)
        if res.returncode != 0:
            raise Unprocessable(f"ffmpeg falhou ao extrair o áudio: {_tail(res.stderr)}")
        if not tmp.is_file() or tmp.stat().st_size <= 44:
            raise Unprocessable("ffmpeg não produziu áudio")
        tmp.replace(dst)
    finally:
        tmp.unlink(missing_ok=True)


def wav_duration(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / float(w.getframerate())
    except (wave.Error, EOFError) as exc:
        raise Unprocessable(f"WAV inválido: {exc}") from None


def _quietest_frame(src: wave.Wave_read, lo: int, hi: int, rate: int) -> int:
    """Frame index in [lo, hi) at the centre of the quietest 100 ms window (16-bit mono only)."""
    window = max(1, rate // 10)
    src.setpos(lo)
    samples = array("h")
    samples.frombytes(src.readframes(hi - lo))
    if sys.byteorder == "big":
        samples.byteswap()
    best, best_energy = hi, None
    for start in range(0, len(samples) - window + 1, window):
        energy = sum(x * x for x in samples[start : start + window])
        if best_energy is None or energy < best_energy:
            best, best_energy = lo + start + window // 2, energy
    return best


def split_wav(path: Path, chunk_seconds: int, out_dir: Path) -> list[tuple[Path, float]]:
    """Cut a PCM WAV into consecutive chunks of ~chunk_seconds; returns (chunk, offset_seconds).

    Keeps whisper-server's per-request memory bounded (it buffers the whole upload plus float32 PCM).
    Each cut lands on the quietest 100 ms of the SILENCE_SEARCH_SECONDS before the nominal boundary,
    so words are not split between chunks. chunk_seconds <= 0 or a short file returns the original.
    """
    try:
        src = wave.open(str(path), "rb")
    except (wave.Error, EOFError) as exc:
        raise Unprocessable(f"WAV inválido: {exc}") from None
    with src:
        rate, total = src.getframerate(), src.getnframes()
        width, channels = src.getsampwidth(), src.getnchannels()
        per_chunk = chunk_seconds * rate
        tail = MIN_TAIL_SECONDS * rate
        if chunk_seconds <= 0 or total <= per_chunk + tail:
            return [(path, 0.0)]
        search = min(SILENCE_SEARCH_SECONDS * rate, per_chunk // 2) if (width, channels) == (2, 1) else 0
        out_dir.mkdir(parents=True, exist_ok=True)
        chunks: list[tuple[Path, float]] = []
        offset = 0
        while offset < total:
            if total - offset <= per_chunk + tail:
                end = total
            else:
                end = offset + per_chunk
                if search:
                    end = _quietest_frame(src, end - search, end, rate)
            out = out_dir / f"chunk-{len(chunks):04d}.wav"
            src.setpos(offset)
            with wave.open(str(out), "wb") as dst:
                dst.setnchannels(channels)
                dst.setsampwidth(width)
                dst.setframerate(rate)
                left = end - offset
                while left > 0:
                    block = src.readframes(min(left, rate * 10))
                    if not block:
                        break
                    dst.writeframes(block)
                    left -= len(block) // (width * channels)
            chunks.append((out, offset / rate))
            offset = end
        return chunks
