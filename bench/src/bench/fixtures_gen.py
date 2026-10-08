"""Media fixtures that are generated instead of committed (`bench fixtures` / `make bench-fixtures`).

- tones: Python stdlib only (always generated);
- speech: espeak-ng, resampled to 16 kHz mono with ffmpeg when available;
- video: espeak-ng + ffmpeg.
Missing tools only skip the files (and the tasks that need them); nothing else depends on them.
"""

import math
import shutil
import struct
import subprocess
import tempfile
import wave
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Tone:
    name: str
    seconds: float
    rate: int = 16000
    freq: float = 440.0
    amplitude: float = 0.3


@dataclass(frozen=True)
class Speech:
    name: str
    text: str
    voice: str = "pt-br"


@dataclass(frozen=True)
class Video:
    name: str
    text: str
    voice: str = "en-us"


GENERATED: dict[str, list[Tone | Speech | Video]] = {
    "media-metadados-audio": [Tone("tom.wav", seconds=12.0, rate=16000, freq=440.0)],
    "media-recado-whatsapp": [Speech("recado.wav", (
        "Oi Leo, aqui é a Fernanda da Nitro. A reunião de alinhamento do aplicativo de entregas foi remarcada "
        "para quinta-feira, às quinze horas, na sala dois. Por favor, leve o relatório de bugs da última sprint. "
        "Obrigada!"))],
    "media-aula-concorrencia": [Speech("aula.wav", (
        "Nesta aula vamos falar sobre concorrência em Go. Uma goroutine é uma função que executa de forma "
        "concorrente. Os canais permitem que as goroutines se comuniquem com segurança. A regra de ouro é: não "
        "comunique compartilhando memória; compartilhe memória comunicando. Para esperar várias goroutines "
        "terminarem, use um WaitGroup."))],
    "media-video-aula-ingles": [Video("aula-ingles.mp4", (
        "Today we will study the present perfect tense. We use the present perfect for life experiences, "
        "for example: I have visited London twice. We form it with have or has, plus the past participle."))],
}


@dataclass(frozen=True)
class Result:
    fixture: str
    name: str
    status: str  # created | exists | skipped | failed
    detail: str = ""


def write_tone(path: Path, tone: Tone) -> None:
    frames = int(tone.seconds * tone.rate)
    peak = int(32767 * tone.amplitude)
    data = b"".join(struct.pack("<h", int(peak * math.sin(2 * math.pi * tone.freq * i / tone.rate)))
                    for i in range(frames))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(tone.rate)
        w.writeframes(data)


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"{cmd[0]} exited {proc.returncode}: {(proc.stderr or proc.stdout).strip()[-300:]}")


def _speech_wav(text: str, voice: str, out: Path, ffmpeg: str | None, espeak: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / "speech.wav"
        _run([espeak, "-v", voice, "-s", "150", "-w", str(raw), text])
        if ffmpeg:
            _run([ffmpeg, "-nostdin", "-loglevel", "error", "-y", "-i", str(raw),
                  "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(out)])
        else:
            shutil.copyfile(raw, out)


def _video(item: Video, out: Path, ffmpeg: str, espeak: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        audio = Path(tmp) / "speech.wav"
        _speech_wav(item.text, item.voice, audio, None, espeak)
        _run([ffmpeg, "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=navy:s=320x240:r=10",
              "-i", str(audio), "-shortest", "-c:v", "mpeg4", "-q:v", "5", "-c:a", "aac", "-b:a", "64k", str(out)])


def generate(fixtures_dir: Path, force: bool = False,
             which: Callable[[str], str | None] | None = None) -> list[Result]:
    which = which or shutil.which
    ffmpeg, espeak = which("ffmpeg"), which("espeak-ng")
    results = []
    for fixture, items in GENERATED.items():
        directory = fixtures_dir / fixture
        directory.mkdir(parents=True, exist_ok=True)
        for item in items:
            out = directory / item.name
            if out.exists() and not force:
                results.append(Result(fixture, item.name, "exists"))
                continue
            missing = []
            if not isinstance(item, Tone) and not espeak:
                missing.append("espeak-ng")
            if isinstance(item, Video) and not ffmpeg:
                missing.append("ffmpeg")
            if missing:
                results.append(Result(fixture, item.name, "skipped", f"needs {' + '.join(missing)}"))
                continue
            tmp = out.with_name(out.stem + ".partial" + out.suffix)
            try:
                if isinstance(item, Tone):
                    write_tone(tmp, item)
                elif isinstance(item, Speech):
                    _speech_wav(item.text, item.voice, tmp, ffmpeg, espeak)
                else:
                    _video(item, tmp, ffmpeg, espeak)
                tmp.replace(out)
                results.append(Result(fixture, item.name, "created"))
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                tmp.unlink(missing_ok=True)
                results.append(Result(fixture, item.name, "failed", str(exc)))
    return results


def missing_files(fixtures_dir: Path, fixture: str | None, names: tuple[str, ...]) -> list[str]:
    if not names:
        return []
    return [n for n in names if not (fixtures_dir / (fixture or "") / n).is_file()]
