"""File parsers: each turns a file into normalized text, sections for the chunker and metadata."""

import csv
import datetime as dt
import io
import logging
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from kb.chunker import Section, fence_closes, fence_open, split_markdown_sections

log = logging.getLogger(__name__)

PARSER_VERSION = "1"
CSV_MAX_ROWS = 50


@dataclass
class ParsedDoc:
    title: str
    content: str  # normalized full text; its hash decides whether to re-embed
    sections: list[Section]
    mime_type: str
    kind: str  # markdown | text | transcript | pdf | csv
    metadata: dict = field(default_factory=dict)


class UnsupportedFile(Exception):
    pass


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def normalize(text: str) -> str:
    """NFC, LF newlines, no trailing spaces, at most one blank line in a row (outside code fences)."""
    text = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = []
    fence: str | None = None
    for line in text.split("\n"):
        line = line.rstrip()
        if fence:
            if fence_closes(line, fence):
                fence = None
        elif marker := fence_open(line):
            fence = marker
        elif not line and out and not out[-1]:
            continue
        out.append(line)
    return "\n".join(out).strip()


def json_safe(value):
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(v) for v in value]
    if isinstance(value, (dt.date, dt.datetime, dt.time)):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


# ── Markdown / Obsidian ──────────────────────────────────────────────────────

_FRONTMATTER = re.compile(r"\A---[ \t]*\n(.*?)\n(?:---|\.\.\.)[ \t]*(?:\n|\Z)", re.DOTALL)
_COMMENT = re.compile(r"%%.*?%%", re.DOTALL)
_EMBED = re.compile(r"!\[\[([^\]\n]+)\]\]")
_WIKILINK = re.compile(r"\[\[([^\]\n]+)\]\]")
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_TAG = re.compile(r"(?<![\w/#&\[\]()\"'`])#([\w][\w/-]*)")


def split_frontmatter(text: str) -> tuple[dict, str]:
    m = _FRONTMATTER.match(text)
    if not m:
        return {}, text
    try:
        data = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as exc:
        log.warning("invalid YAML frontmatter ignored: %s", exc)
        data = {}
    if not isinstance(data, dict):
        data = {}
    return data, text[m.end() :]


def _map_outside_fences(text: str, fn: Callable[[str], str]) -> str:
    """Apply `fn` to every run of lines that is not inside a fenced code block."""
    out: list[str] = []
    prose: list[str] = []
    fence: str | None = None

    def flush() -> None:
        if prose:
            out.append(fn("\n".join(prose)))
            prose.clear()

    for line in text.split("\n"):
        if fence:
            out.append(line)
            if fence_closes(line, fence):
                fence = None
        elif marker := fence_open(line):
            flush()
            fence = marker
            out.append(line)
        else:
            prose.append(line)
    flush()
    return "\n".join(out)


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [v for v in re.split(r"[,\s]+", value) if v]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if v is not None and str(v).strip()]
    return [str(value)]


def _dedupe(items: list[str], casefold: bool = False) -> list[str]:
    seen, out = set(), []
    for item in items:
        key = item.casefold() if casefold else item
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _link_target(inner: str) -> tuple[str, str]:
    """'Note#Heading|Alias' -> ('Note', display text)."""
    target, _, alias = inner.partition("|")
    note, _, anchor = target.partition("#")
    note, anchor = note.strip(), anchor.lstrip("^").strip()
    if alias.strip():
        display = alias.strip()
    elif anchor and not target.split("#", 1)[1].startswith("^"):
        display = f"{note} > {anchor}" if note else anchor
    else:
        display = note
    return note, display


def parse_markdown_text(text: str, fallback_title: str) -> ParsedDoc:
    frontmatter, body = split_frontmatter(normalize(text))
    links: list[str] = []
    embeds: list[str] = []
    tags = [t.lstrip("#") for t in _as_list(frontmatter.get("tags") or frontmatter.get("tag"))]

    def transform(prose: str) -> str:
        prose = _COMMENT.sub("", prose)
        embeds.extend(_link_target(m)[0] for m in _EMBED.findall(prose))
        prose = _EMBED.sub("", prose)

        def render(m: re.Match) -> str:
            note, display = _link_target(m.group(1))
            if note:
                links.append(note)
            return display

        prose = _WIKILINK.sub(render, prose)
        tags.extend(m.group(1) for m in _TAG.finditer(_INLINE_CODE.sub("", prose)) if not m.group(1).isdigit())
        return prose

    content = normalize(_map_outside_fences(body, transform))
    sections = split_markdown_sections(content)
    h1 = next((s.heading_path[0] for s in sections if s.level == 1), "")
    title = str(frontmatter.get("title") or h1 or fallback_title).strip()
    aliases = frontmatter.get("aliases") or frontmatter.get("alias")

    metadata: dict = {
        "tags": _dedupe(tags, casefold=True),
        "aliases": [aliases] if isinstance(aliases, str) else _as_list(aliases),
        "created": json_safe(frontmatter.get("created") or frontmatter.get("date")),
        "links": _dedupe(links),
        "embeds": _dedupe(embeds),
        "frontmatter": json_safe(frontmatter),
    }
    metadata = {k: v for k, v in metadata.items() if v}
    return ParsedDoc(title, content, sections, "text/markdown", "markdown", metadata)


def parse_markdown(path: Path) -> ParsedDoc:
    return parse_markdown_text(read_text(path), path.stem)


# ── Plain text ───────────────────────────────────────────────────────────────


def parse_text(path: Path) -> ParsedDoc:
    content = normalize(read_text(path))
    return ParsedDoc(path.stem, content, [Section(text=content)], "text/plain", "text")


# ── Transcripts (.srt / .vtt) ───────────────────────────────────────────────

_TIMESTAMP = re.compile(r"-->")
_CUE_TAG = re.compile(r"<[^>]+>|\{\\[^}]*\}")


def clean_subtitles(text: str) -> str:
    """Drop headers, cue numbers, timestamps and markup; merge cue text into paragraphs."""
    paragraphs: list[str] = []
    current: list[str] = []
    skip_block = False
    lines = normalize(text).split("\n")
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            skip_block = False
            continue
        if stripped.startswith("WEBVTT") or stripped.split(" ", 1)[0] in {"NOTE", "STYLE", "REGION"}:
            skip_block = True
            continue
        if skip_block or _TIMESTAMP.search(stripped):
            continue
        if stripped.isdigit() and i + 1 < len(lines) and _TIMESTAMP.search(lines[i + 1]):
            continue
        cue = re.sub(r"\s+", " ", _CUE_TAG.sub("", stripped)).strip()
        if cue and (not current or current[-1] != cue):
            current.append(cue)
        if cue.endswith((".", "!", "?", "…")) and sum(len(c) for c in current) > 600:
            paragraphs.append(" ".join(current))
            current = []
    if current:
        paragraphs.append(" ".join(current))
    return "\n\n".join(paragraphs)


def parse_transcript(path: Path) -> ParsedDoc:
    content = normalize(clean_subtitles(read_text(path)))
    mime = "text/vtt" if path.suffix.lower() == ".vtt" else "application/x-subrip"
    return ParsedDoc(path.stem, content, [Section(text=content)], mime, "transcript")


# ── PDF ──────────────────────────────────────────────────────────────────────


def parse_pdf(path: Path) -> ParsedDoc:
    from pypdf import PdfReader

    reader = PdfReader(path)
    if reader.is_encrypted and not reader.decrypt(""):
        raise UnsupportedFile("encrypted PDF")
    sections, pages = [], []
    for number, page in enumerate(reader.pages, start=1):
        text = normalize(page.extract_text() or "")
        if text:
            sections.append(Section(text=text, metadata={"page": number}))
            pages.append(text)
    info_title = (reader.metadata.title if reader.metadata else None) or ""
    metadata = {"pages": len(reader.pages)}
    return ParsedDoc(info_title.strip() or path.stem, "\n\n".join(pages), sections, "application/pdf", "pdf", metadata)


# ── CSV ──────────────────────────────────────────────────────────────────────


def render_csv(text: str, max_rows: int = CSV_MAX_ROWS) -> tuple[str, dict]:
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = [r for r in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in r)]
    if not rows:
        return "", {"columns": [], "rows": 0}
    header, data = rows[0], rows[1:]
    shown = data[:max_rows]
    lines = [" | ".join(header)] + [" | ".join(row) for row in shown]
    meta = {"columns": header, "rows": len(data), "truncated": len(data) > max_rows}
    if meta["truncated"]:
        lines.append(f"(truncado: mostrando {len(shown)} de {len(data)} linhas)")
    return "\n".join(lines), meta


def parse_csv(path: Path) -> ParsedDoc:
    content, metadata = render_csv(read_text(path))
    content = normalize(content)
    # one block per row group so the chunker can split large tables at row boundaries
    sections = [Section(text=content.replace("\n", "\n\n"))]
    return ParsedDoc(path.stem, content, sections, "text/csv", "csv", metadata)


PARSERS: dict[str, Callable[[Path], ParsedDoc]] = {
    ".md": parse_markdown,
    ".markdown": parse_markdown,
    ".txt": parse_text,
    ".srt": parse_transcript,
    ".vtt": parse_transcript,
    ".pdf": parse_pdf,
    ".csv": parse_csv,
}


def is_supported(path: Path) -> bool:
    return path.suffix.lower() in PARSERS


def parse_file(path: Path) -> ParsedDoc:
    parser = PARSERS.get(path.suffix.lower())
    if parser is None:
        raise UnsupportedFile(path.suffix or "no extension")
    return parser(path)
