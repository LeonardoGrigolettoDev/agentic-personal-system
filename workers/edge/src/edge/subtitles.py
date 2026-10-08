"""Transcript renderings: plain text (one segment per line, like whisper-cli -otxt) and SRT."""

from collections.abc import Sequence

from edge.whisper import Segment


def srt_timestamp(seconds: float) -> str:
    ms = max(0, round(seconds * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _line(seg: Segment) -> str:
    return f"[{seg.speaker}] {seg.text}" if seg.speaker else seg.text


def render_txt(segments: Sequence[Segment]) -> str:
    return "\n".join(_line(s) for s in segments) + ("\n" if segments else "")


def render_srt(segments: Sequence[Segment]) -> str:
    blocks = [
        f"{i}\n{srt_timestamp(s.start)} --> {srt_timestamp(max(s.end, s.start))}\n{_line(s)}\n"
        for i, s in enumerate(segments, start=1)
    ]
    return "\n".join(blocks)
