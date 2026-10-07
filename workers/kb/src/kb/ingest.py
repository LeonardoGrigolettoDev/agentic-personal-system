"""Ingest pipeline: discover -> guard -> parse -> hash -> (skip | chunk + embed + replace) -> prune."""

import hashlib
import logging
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from kb import chunker
from kb.chunker import Chunk
from kb.parsers import PARSER_VERSION, ParsedDoc, UnsupportedFile, is_supported, parse_file
from kb.sources import (
    Discovery,
    SourceRef,
    check_tenant,
    discover,
    guard_root,
    iter_sources,
    prune_prefixes,
)
from kb.store import DocState, DocumentRow

log = logging.getLogger(__name__)

PIPELINE_VERSION = (
    f"kb-parser/{PARSER_VERSION};chunk/{chunker.MAX_TOKENS}/{chunker.OVERLAP_TOKENS}/{chunker.MERGE_BELOW_TOKENS}"
)
SUBTITLE_SUFFIXES = {".srt", ".vtt"}


class Store(Protocol):
    def get_state(self, tenant: str, source_uri: str) -> DocState | None: ...
    def write_document(self, doc: DocumentRow, chunks: Sequence[Chunk], vectors: Sequence[Sequence[float]]) -> str: ...
    def update_metadata(self, doc: DocumentRow) -> bool: ...
    def prune(self, tenant: str, prefixes: Sequence[str], keep: set[str]) -> list[str]: ...
    def record_ingest(self, tenant: str, payload: dict) -> None: ...


class DocumentEmbedder(Protocol):
    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...


@dataclass(frozen=True)
class IngestOptions:
    tenant: str
    domain: str | None = None
    label: str | None = None
    source_type: str | None = None  # override auto-detected 'obsidian' / 'file'
    prune: bool = False
    dry_run: bool = False


@dataclass
class IngestReport:
    counts: Counter = field(default_factory=Counter)
    actions: list[tuple[str, str, str]] = field(default_factory=list)  # (action, uri, detail)
    pruned: list[str] = field(default_factory=list)

    def add(self, action: str, uri: str, detail: str = "") -> None:
        self.counts[action] += 1
        self.actions.append((action, uri, detail))

    def summary(self) -> dict:
        return {**dict(sorted(self.counts.items())), "pruned": len(self.pruned)}


def content_hash(content: str) -> str:
    return hashlib.sha256(f"{PIPELINE_VERSION}\n{content}".encode()).hexdigest()


def embedding_input(title: str, chunk: Chunk) -> str:
    """Prefix the breadcrumb (and title, unless it already leads the breadcrumb) to the chunk text."""
    crumbs = chunk.heading_path.split(" > ") if chunk.heading_path else []
    if title and not (crumbs and crumbs[0].casefold() == title.casefold()):
        crumbs.insert(0, title)
    header = " > ".join(crumbs)
    return f"{header}\n\n{chunk.content}" if header else chunk.content


def _duplicate_subtitle(path: Path) -> bool:
    """whisper writes x.txt and x.srt side by side; index only the .txt."""
    return path.suffix.lower() in SUBTITLE_SUFFIXES and path.with_suffix(".txt").is_file()


def _document_row(ref: SourceRef, parsed: ParsedDoc, opts: IngestOptions, digest: str) -> DocumentRow:
    metadata = {**parsed.metadata, "kind": parsed.kind, "parser": PIPELINE_VERSION}
    if ref.vault:
        metadata["vault"] = ref.vault
    return DocumentRow(
        tenant=opts.tenant,
        domain=opts.domain,
        source_type=opts.source_type or ref.source_type,
        source_uri=ref.uri,
        title=parsed.title,
        mime_type=parsed.mime_type,
        content_hash=digest,
        metadata=metadata,
    )


def prepare(root: Path, opts: IngestOptions) -> tuple[Discovery, list[SourceRef]]:
    """Discover files and enforce the tenant guard before anything is parsed or written."""
    if not root.exists():
        raise FileNotFoundError(root)
    discovery = discover(root)
    guard_root(root, opts.tenant, discovery)
    refs = list(iter_sources(root, opts.label, discovery))
    for ref in refs:
        check_tenant(ref.vault, opts.tenant)
    return discovery, refs


def ingest(
    root: Path,
    opts: IngestOptions,
    store: Store | None = None,
    embedder: DocumentEmbedder | None = None,
) -> IngestReport:
    discovery, refs = prepare(root, opts)
    if not opts.dry_run and (store is None or embedder is None):
        raise ValueError("store and embedder are required unless dry_run")

    report = IngestReport()
    seen: set[str] = set()
    for ref in refs:
        if not is_supported(ref.path):
            log.info("skip (unsupported type): %s", ref.uri)
            report.add("unsupported", ref.uri)
            continue
        if _duplicate_subtitle(ref.path):
            log.info("skip (subtitle duplicates .txt transcript): %s", ref.uri)
            report.add("duplicate", ref.uri)
            continue
        seen.add(ref.uri)
        try:
            parsed = parse_file(ref.path)
        except (UnsupportedFile, OSError, ValueError) as exc:
            log.warning("skip (parse error) %s: %s", ref.uri, exc)
            report.add("error", ref.uri, str(exc))
            continue
        except Exception as exc:  # third-party parsers (pypdf) raise a zoo of exceptions
            log.warning("skip (parse error) %s: %s: %s", ref.uri, type(exc).__name__, exc)
            report.add("error", ref.uri, f"{type(exc).__name__}: {exc}")
            continue
        _ingest_one(ref, parsed, opts, store, embedder, report)

    if opts.prune:
        prefixes = prune_prefixes(root, opts.label, discovery)
        if opts.dry_run:
            for prefix in prefixes:
                report.add("would-prune-under", prefix, f"keeping {len(seen)} seen documents")
        else:
            report.pruned = store.prune(opts.tenant, prefixes, seen)
            for uri in report.pruned:
                log.info("pruned: %s", uri)
    if not opts.dry_run:
        store.record_ingest(opts.tenant, {"root": prune_prefixes(root, opts.label, discovery), **report.summary()})
    return report


def _ingest_one(
    ref: SourceRef,
    parsed: ParsedDoc,
    opts: IngestOptions,
    store: Store | None,
    embedder: DocumentEmbedder | None,
    report: IngestReport,
) -> None:
    digest = content_hash(parsed.content)
    doc = _document_row(ref, parsed, opts, digest)
    if opts.dry_run:
        chunks = chunker.chunk_sections(parsed.sections)
        tokens = sum(c.token_count for c in chunks)
        report.add("plan", ref.uri, f"{len(chunks)} chunks, ~{tokens} tokens, {doc.source_type}, sha256 {digest[:12]}")
        return

    state = store.get_state(opts.tenant, ref.uri)
    if state and state.status == "active" and state.content_hash == digest and not state.stale_embeddings:
        action = "metadata" if store.update_metadata(doc) else "unchanged"
        report.add(action, ref.uri)
        return

    chunks = chunker.chunk_sections(parsed.sections)
    vectors = embedder.embed_documents([embedding_input(parsed.title, c) for c in chunks]) if chunks else []
    store.write_document(doc, chunks, vectors)
    action = "new" if state is None else "updated"
    log.info("%s: %s (%d chunks)", action, ref.uri, len(chunks))
    report.add(action, ref.uri, f"{len(chunks)} chunks")


ALLOWED_URI_SCHEMES = ("obsidian://", "file://", "storage://", "https://", "http://", "agent://", "manual://")


@dataclass(frozen=True)
class ContentIn:
    """A document handed over by another service (edge transcripts, agent notes) instead of a file."""

    tenant: str
    source_uri: str
    title: str
    content: str
    source_type: str = "agent"
    domain: str | None = None
    metadata: dict = field(default_factory=dict)


def validate_content(item: ContentIn) -> None:
    from kb.sources import DOMAINS, SOURCE_TYPES, TENANTS

    if item.tenant not in TENANTS:
        raise ValueError(f"unknown tenant {item.tenant!r}")
    if item.domain is not None and item.domain not in DOMAINS:
        raise ValueError(f"unknown domain {item.domain!r}")
    if item.source_type not in SOURCE_TYPES:
        raise ValueError(f"unknown source_type {item.source_type!r}")
    if not item.source_uri.startswith(ALLOWED_URI_SCHEMES) or item.source_uri.startswith("file:///"):
        raise ValueError("source_uri must be an abstract reference (storage://, obsidian://, agent://, https://...)")
    if not item.content.strip():
        raise ValueError("content is empty")


def ingest_content(item: ContentIn, store: Store, embedder: DocumentEmbedder) -> tuple[str, str]:
    """Idempotent on (tenant, source_uri, content hash). Returns (action, source_uri)."""
    from kb.parsers import parse_markdown_text

    validate_content(item)
    parsed = parse_markdown_text(item.content, item.title or item.source_uri)
    parsed.metadata.update(item.metadata)
    ref = SourceRef(path=Path("."), uri=item.source_uri, source_type=item.source_type, vault=None)
    opts = IngestOptions(tenant=item.tenant, domain=item.domain, source_type=item.source_type)
    report = IngestReport()
    if item.title:
        parsed.title = item.title
    _ingest_one(ref, parsed, opts, store, embedder, report)
    action = report.actions[-1][0] if report.actions else "unchanged"
    return action, item.source_uri
