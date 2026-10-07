"""Heading-aware, deterministic chunking.

Sections (markdown headings, PDF pages, whole files) are split into blocks (paragraphs or fenced
code blocks) and packed greedily into chunks of at most `max_tokens`. A heading starts a new chunk
unless the current one is still tiny (< `merge_below` tokens), in which case the small section is
merged forward with its heading kept inline. Splits inside a section carry ~`overlap_tokens` of
trailing prose into the next chunk. Tokens are estimated as len(text) / 4.
"""

import math
import re
from dataclasses import dataclass, field

MAX_TOKENS = 500
OVERLAP_TOKENS = 50
MERGE_BELOW_TOKENS = 100
CODE_MAX_FACTOR = 2  # a fenced block up to 2x max_tokens stays whole rather than being split

_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$")
_FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})(.*)$")
_SENTENCE_END = re.compile(r"(?<=[.!?…:;])\s+")


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / 4)


@dataclass(frozen=True)
class Section:
    heading_path: tuple[str, ...] = ()
    text: str = ""
    level: int = 0  # heading level (0 = no heading: preamble, page, whole file)
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Chunk:
    index: int
    content: str
    heading_path: str | None
    token_count: int
    metadata: dict


def fence_open(line: str) -> str | None:
    """Return the fence marker (e.g. ``` or ~~~~) if `line` opens a fenced code block."""
    m = _FENCE.match(line)
    if not m:
        return None
    marker, info = m.groups()
    if marker[0] == "`" and "`" in info:
        return None
    return marker


def fence_closes(line: str, marker: str) -> bool:
    stripped = line.strip()
    return stripped.startswith(marker) and set(stripped) == {marker[0]}


def split_markdown_sections(body: str) -> list[Section]:
    """Split markdown on ATX headings (ignoring '#' lines inside fenced code)."""
    sections: list[Section] = []
    stack: list[tuple[int, str]] = []
    lines: list[str] = []
    level = 0
    fence: str | None = None

    def flush() -> None:
        sections.append(Section(tuple(t for _, t in stack), "\n".join(lines).strip("\n"), level))

    for line in body.split("\n"):
        if fence:
            if fence_closes(line, fence):
                fence = None
        elif marker := fence_open(line):
            fence = marker
        elif m := _HEADING.match(line):
            flush()
            level, title = len(m.group(1)), m.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            lines = []
            continue
        lines.append(line)
    flush()
    return [s for s in sections if s.text.strip() or s.level]


def split_blocks(text: str) -> list[str]:
    """Paragraphs separated by blank lines; a fenced code block is always one block."""
    blocks: list[str] = []
    current: list[str] = []
    fence: str | None = None

    def flush() -> None:
        block = "\n".join(current).strip("\n")
        if block.strip():
            blocks.append(block)
        current.clear()

    for line in text.split("\n"):
        if fence:
            current.append(line)
            if fence_closes(line, fence):
                fence = None
                flush()
        elif marker := fence_open(line):
            flush()
            fence = marker
            current.append(line)
        elif not line.strip():
            flush()
        else:
            current.append(line)
    flush()
    return blocks


def is_code_block(block: str) -> bool:
    return fence_open(block.split("\n", 1)[0]) is not None


def _split_code(block: str, limit: int) -> list[str]:
    lines = block.split("\n")
    opener = lines[0]
    marker = fence_open(opener) or "```"
    body = lines[1:-1] if len(lines) > 1 and fence_closes(lines[-1], marker) else lines[1:]
    budget = max(1, limit - len(opener) - len(marker) - 2)
    pieces, current, size = [], [], 0
    for line in body:
        if current and size + len(line) + 1 > budget:
            pieces.append(current)
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        pieces.append(current)
    return ["\n".join([opener, *piece, marker]) for piece in pieces]


def _pack(units: list[str], limit: int, sep: str) -> list[str]:
    out, current = [], ""
    for unit in units:
        candidate = f"{current}{sep}{unit}" if current else unit
        if current and len(candidate) > limit:
            out.append(current)
            current = unit
        else:
            current = candidate
    if current:
        out.append(current)
    return out


def _split_prose(block: str, limit: int) -> list[str]:
    units: list[str] = []
    for sentence in (s for line in block.split("\n") for s in _SENTENCE_END.split(line) if s.strip()):
        if len(sentence) <= limit:
            units.append(sentence)
            continue
        for word_group in _pack(sentence.split(), limit, " "):
            units.extend(word_group[i : i + limit] for i in range(0, len(word_group), limit))
    return _pack(units, limit, " ")


def split_oversize(block: str, limit: int, code_limit: int) -> list[str]:
    if is_code_block(block):
        return [block] if len(block) <= code_limit else _split_code(block, limit)
    return [block] if len(block) <= limit else _split_prose(block, limit)


def overlap_tail(text: str, max_chars: int) -> str:
    """Trailing ~max_chars of prose, starting at a sentence or word boundary. Never from code."""
    if max_chars <= 0 or is_code_block(text) or text.rstrip().endswith(("```", "~~~")):
        return ""
    if len(text) <= max_chars:
        return text.strip()
    tail = text[-max_chars:]
    m = _SENTENCE_END.search(tail)
    if m and len(tail) - m.end() >= max_chars // 4:
        return tail[m.end() :].strip()
    space = re.search(r"\s", tail)
    return tail[space.end() :].strip() if space else ""


@dataclass
class _Draft:
    blocks: list[str] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)
    has_content: bool = False

    @property
    def chars(self) -> int:
        return sum(len(b) for b in self.blocks) + 2 * max(0, len(self.blocks) - 1)

    def add(self, block: str, section: Section, content: bool = True) -> None:
        self.blocks.append(block)
        if section not in self.sections:
            self.sections.append(section)
        self.has_content |= content


def _common_prefix(paths: list[tuple[str, ...]]) -> tuple[str, ...]:
    prefix = paths[0]
    for path in paths[1:]:
        n = 0
        while n < min(len(prefix), len(path)) and prefix[n] == path[n]:
            n += 1
        prefix = prefix[:n]
    return prefix


def _chunk_metadata(sections: list[Section]) -> dict:
    pages = sorted({s.metadata["page"] for s in sections if "page" in s.metadata})
    meta: dict = {}
    if pages:
        meta["page"] = pages[0]
        if pages[-1] != pages[0]:
            meta["page_end"] = pages[-1]
    return meta


def chunk_sections(
    sections: list[Section],
    max_tokens: int = MAX_TOKENS,
    overlap_tokens: int = OVERLAP_TOKENS,
    merge_below: int = MERGE_BELOW_TOKENS,
) -> list[Chunk]:
    max_chars, overlap_chars, merge_chars = max_tokens * 4, overlap_tokens * 4, merge_below * 4
    piece_limit = max(1, max_chars - overlap_chars - 2)  # room for the overlap prefix
    code_limit = max_chars * CODE_MAX_FACTOR
    drafts: list[_Draft] = []
    current = _Draft()

    def flush() -> None:
        nonlocal current
        if current.has_content:
            drafts.append(current)
        current = _Draft()

    for section in sections:
        blocks = split_blocks(section.text)
        if not blocks:
            continue
        heading = f"{'#' * section.level} {section.heading_path[-1]}" if section.level else None
        first = len(blocks[0]) + (len(heading) + 2 if heading else 0)
        if current.has_content and current.chars < merge_chars and current.chars + 2 + first <= max_chars:
            if heading:
                current.add(heading, section, content=False)
        else:
            flush()
        for block in blocks:
            for piece in split_oversize(block, piece_limit, code_limit):
                if current.blocks and current.chars + 2 + len(piece) > max_chars:
                    tail = overlap_tail(current.blocks[-1], overlap_chars)
                    flush()
                    if tail:
                        current.add(tail, section, content=False)
                current.add(piece, section)
    flush()

    chunks = []
    for draft in drafts:
        content = "\n\n".join(draft.blocks).strip()
        if not content:
            continue
        path = _common_prefix([s.heading_path for s in draft.sections])
        chunks.append(
            Chunk(
                index=len(chunks),
                content=content,
                heading_path=" > ".join(path) or None,
                token_count=estimate_tokens(content),
                metadata=_chunk_metadata(draft.sections),
            )
        )
    return chunks
