"""Media operations behind the HTTP API: extract -> transcribe -> summarize -> ingest -> archive.

Every operation is synchronous (subprocess + blocking HTTP) and runs on the job queue's worker thread.
Inputs are storage:// URIs in the `media` bucket; outputs are written next to them:
  media/processing/<id>.wav                         /extract_audio output (16 kHz mono WAV, kept for the caller)
  media/processing/<id>.<job>.{wav,chunks}          transcription scratch, private to one job and deleted by it
  media/transcripts/<id>.{txt,srt,json}             transcript (+ segments and reuse metadata)
  media/transcripts/<id>.summary.{md,json}          summary/notes/tasks/topics
"""

import hashlib
import json
import logging
import os
import re
import shutil
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath

from edge import diarize as diarization
from edge import media
from edge.config import Settings
from edge.errors import BadRequest, NotFound, TooLarge, Unavailable, Unprocessable, UpstreamError, UpstreamTimeout
from edge.knowledge import KnowledgeClient
from edge.llm import LLMClient, LLMError
from edge.storage import LocalStorage, StorageError, make_uri, parse_uri
from edge.subtitles import render_srt, render_txt
from edge.summarize import Summarizer, render_markdown
from edge.whisper import Segment, WhisperClient

log = logging.getLogger(__name__)

MEDIA_BUCKET = "media"
DOMAINS = ("chief", "engineering", "finance", "projects", "personal", "learning")  # = kb.sources.DOMAINS
MAX_TRANSCRIPT_BYTES = 8 * 1024 * 1024
_UUID_HEX = re.compile(r"^[0-9a-f]{32}$")
_HASH_CHARS = 12
_OWN_ID = re.compile(rf"^[a-z0-9][a-z0-9-]{{0,40}}-[0-9a-f]{{{_HASH_CHARS}}}$")  # what media_id() generates
_SAMPLE_BYTES = 64 * 1024


def media_id(uri: str, path: Path) -> str:
    """Id of one version of a source: the upload uuid, the id of our own processing WAV, or slug + hash.

    Uploads get a fresh uuid per file. Any other name can be reused (`aula05.m4a` dropped again, phone
    exports), so the hash covers the URI plus the file's size, mtime and first/last 64 KB: a new recording
    at the same path gets a new id instead of overwriting the previous transcript and KB documents.
    """
    _, key = parse_uri(uri)
    stem = PurePosixPath(key).stem
    own_wav = key.startswith("processing/") and key.endswith(".wav") and _OWN_ID.fullmatch(stem)
    if _UUID_HEX.fullmatch(stem) or own_wav:
        return stem
    ascii_stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_stem).strip("-")[:40].rstrip("-") or "media"
    return f"{slug}-{_version_hash(uri, path)[:_HASH_CHARS]}"


def _version_hash(uri: str, path: Path) -> str:
    st = path.stat()
    digest = hashlib.sha256(f"{uri}\0{st.st_size}\0{st.st_mtime_ns}\0".encode())
    with path.open("rb") as fh:
        digest.update(fh.read(_SAMPLE_BYTES))
        if st.st_size > 2 * _SAMPLE_BYTES:
            fh.seek(-_SAMPLE_BYTES, os.SEEK_END)
            digest.update(fh.read(_SAMPLE_BYTES))
    return digest.hexdigest()


def _transcript_uris(mid: str) -> dict[str, str]:
    base = f"transcripts/{mid}"
    return {
        "txt": make_uri(MEDIA_BUCKET, f"{base}.txt"),
        "srt": make_uri(MEDIA_BUCKET, f"{base}.srt"),
        "json": make_uri(MEDIA_BUCKET, f"{base}.json"),
        "summary_md": make_uri(MEDIA_BUCKET, f"{base}.summary.md"),
        "summary_json": make_uri(MEDIA_BUCKET, f"{base}.summary.json"),
    }


def _llm_error(exc: LLMError) -> Exception:
    if exc.timeout:
        return UpstreamTimeout(f"LiteLLM não respondeu a tempo ({exc})")
    if exc.status is None:
        return Unavailable(f"LiteLLM indisponível ({exc})")
    return UpstreamError(f"LiteLLM falhou ({exc})")


@dataclass
class Edge:
    settings: Settings
    storage: LocalStorage
    whisper: WhisperClient
    llm: LLMClient
    knowledge: KnowledgeClient
    summarizer: Summarizer
    _diarizer: diarization.Diarizer | None = field(default=None, repr=False)

    @classmethod
    def from_settings(cls, s: Settings) -> "Edge":
        llm = LLMClient(s.litellm_base_url, s.litellm_key, s.llm_timeout)
        return cls(
            settings=s,
            storage=LocalStorage(s.storage_root),
            whisper=WhisperClient(s.whisper_url, s.whisper_timeout),
            llm=llm,
            knowledge=KnowledgeClient(s.knowledge_url, s.knowledge_key, s.knowledge_timeout),
            summarizer=Summarizer(
                llm,
                default_model=s.summary_model,
                fallback_model=s.fallback_model,
                map_chunk_tokens=s.map_chunk_tokens,
                fallback_chunk_tokens=s.fallback_chunk_tokens,
                local_max_tokens=s.local_max_tokens,
            ),
        )

    def close(self) -> None:
        for client in (self.whisper, self.llm, self.knowledge):
            client.close()

    # ------------------------------------------------------------ validation (cheap: called before queueing)
    def source_path(self, uri: str) -> tuple[str, Path]:
        try:
            bucket, key = parse_uri(uri)
            path = self.storage.path(uri)
        except StorageError as exc:
            raise BadRequest(f"URI inválida: {exc}") from None
        if bucket != MEDIA_BUCKET:
            raise BadRequest("a fonte precisa estar no bucket media (storage://media/...)")
        if key.endswith(".part") or not path.is_file():
            raise NotFound(f"arquivo não encontrado: {uri}")
        return key, path

    def check_tenant(self, tenant: str, domain: str | None) -> None:
        if tenant not in self.settings.tenants:
            raise BadRequest(f"tenant desconhecido {tenant!r} (válidos: {', '.join(self.settings.tenants)})")
        if domain is not None and domain not in DOMAINS:
            raise BadRequest(f"domain desconhecido {domain!r} (válidos: {', '.join(DOMAINS)})")

    def diarizer(self) -> diarization.Diarizer:
        diarization.require(self.settings.hf_token)
        if self._diarizer is None:
            self._diarizer = diarization.PyannoteDiarizer(self.settings.diarize_model, self.settings.hf_token or "")
        return self._diarizer

    # ------------------------------------------------------------ operations
    def extract_audio(self, source: str) -> dict:
        _, src = self.source_path(source)
        mid = media_id(source, src)
        out_uri = make_uri(MEDIA_BUCKET, f"processing/{mid}.wav")
        return {"id": mid, "source": source, "uri": out_uri, **self._extract(src, self.storage.path(out_uri))}

    def _extract(self, src: Path, out: Path) -> dict:
        info = media.probe(src)
        if not info.has_audio:
            raise Unprocessable("a mídia não tem faixa de áudio")
        media.extract_audio(src, out, self.settings.ffmpeg_timeout)
        return {
            "duration_s": round(media.wav_duration(out), 3),
            "media_duration_s": info.duration_s,
            "has_video": info.has_video,
            "sample_rate": media.SAMPLE_RATE,
            "channels": 1,
        }

    def transcribe(self, source: str, language: str = "pt", diarize: bool = False, force: bool = False) -> dict:
        diarizer = self.diarizer() if diarize else None
        key, src = self.source_path(source)
        mid = media_id(source, src)
        uris = _transcript_uris(mid)
        st = src.stat()
        fingerprint = {
            "source": source,
            "source_size": st.st_size,
            "source_mtime_ns": st.st_mtime_ns,
            "language": language,
            "diarize": diarize,
        }
        if not force and (cached := self._cached_transcript(uris, fingerprint)):
            return cached

        started = time.monotonic()
        own_wav = not (key.startswith("processing/") and key.endswith(".wav"))
        # Job-private scratch: never the /extract_audio output, never shared with a concurrent job on this source.
        scratch = f"processing/{mid}.{uuid.uuid4().hex[:8]}"
        wav = self.storage.path(make_uri(MEDIA_BUCKET, f"{scratch}.wav")) if own_wav else src
        chunk_dir = self.storage.path(make_uri(MEDIA_BUCKET, f"{scratch}.chunks"))
        try:
            if own_wav:
                self._extract(src, wav)
            segments: list[Segment] = []
            chunks = media.split_wav(wav, self.settings.whisper_chunk_seconds, chunk_dir)
            for chunk, offset in chunks:
                segments.extend(s.shifted(offset) for s in self.whisper.transcribe(chunk, language))
            duration = media.wav_duration(wav)
            if diarizer is not None:
                segments = diarization.assign_speakers(segments, diarizer(wav))
        finally:
            shutil.rmtree(chunk_dir, ignore_errors=True)
            if own_wav:
                wav.unlink(missing_ok=True)

        text = render_txt(segments)
        record = {
            "id": mid,
            **fingerprint,
            "duration_s": round(duration, 3),
            "created_at": time.time(),
            "segments": [s.to_dict() for s in segments],
        }
        self.storage.put_text(uris["txt"], text)
        self.storage.put_text(uris["srt"], render_srt(segments))
        self.storage.put_text(uris["json"], json.dumps(record, ensure_ascii=False))
        elapsed = time.monotonic() - started
        log.info(
            "transcribed",
            extra={
                "media_id": mid,
                "duration_s": round(duration, 1),
                "chunks": len(chunks),
                "segments": len(segments),
                "seconds": round(elapsed, 1),
            },
        )
        return self._transcript_response(record, text, uris, cached=False)

    def _cached_transcript(self, uris: dict, fingerprint: dict) -> dict | None:
        if not all(self.storage.exists(uris[k]) for k in ("txt", "srt", "json")):
            return None
        try:
            record = json.loads(self.storage.get_text(uris["json"]))
        except (OSError, ValueError):
            return None
        if any(record.get(k) != v for k, v in fingerprint.items()):
            return None
        log.info("transcript reused", extra={"media_id": record.get("id")})
        return self._transcript_response(record, self.storage.get_text(uris["txt"]), uris, cached=True)

    @staticmethod
    def _transcript_response(record: dict, text: str, uris: dict, *, cached: bool) -> dict:
        return {
            "id": record["id"],
            "source": record["source"],
            "language": record["language"],
            "duration_s": record["duration_s"],
            "text": text,
            "segments": record["segments"],
            "txt_uri": uris["txt"],
            "srt_uri": uris["srt"],
            "json_uri": uris["json"],
            "cached": cached,
        }

    def summarize(
        self, *, text: str | None = None, transcript_uri: str | None = None, kind: str = "all", model: str | None = None
    ) -> dict:
        if transcript_uri:
            _, path = self.source_path(transcript_uri)
            if path.stat().st_size > MAX_TRANSCRIPT_BYTES:
                raise TooLarge(f"transcrição maior que {MAX_TRANSCRIPT_BYTES // (1024 * 1024)} MB")
            text = path.read_text(encoding="utf-8", errors="replace")
        result = self.summarizer.summarize(text or "", kind, model)
        if transcript_uri:
            result["transcript_uri"] = transcript_uri
        return result

    def _store_summary(self, mid: str, summary: dict, title: str) -> tuple[str, str]:
        uris = _transcript_uris(mid)
        markdown = render_markdown(summary, title)
        self.storage.put_text(uris["summary_md"], markdown)
        self.storage.put_text(uris["summary_json"], json.dumps(summary, ensure_ascii=False))
        return uris["summary_md"], markdown

    def process_video(
        self,
        *,
        source: str,
        language: str = "pt",
        kind: str = "all",
        model: str | None = None,
        diarize: bool = False,
        force: bool = False,
        title: str | None = None,
    ) -> dict:
        key, _ = self.source_path(source)
        transcript = self.transcribe(source, language, diarize, force)
        summary = self.summarize(text=transcript["text"], kind=kind, model=model)
        label = title or _default_title(media.media_kind(key), transcript["id"])
        summary_uri, _ = self._store_summary(transcript["id"], summary, f"Resumo: {label}")
        return {
            "id": transcript["id"],
            "source": source,
            "transcript": transcript,
            "summary": summary,
            "summary_uri": summary_uri,
        }

    def pipeline(
        self,
        *,
        source: str,
        tenant: str,
        domain: str | None = None,
        ingest: bool = True,
        language: str = "pt",
        model: str | None = None,
        title: str | None = None,
        diarize: bool = False,
        force: bool = False,
    ) -> dict:
        self.check_tenant(tenant, domain)
        key, _ = self.source_path(source)
        kind = media.media_kind(key) or "audio"
        transcript = self.transcribe(source, language, diarize, force)
        mid = transcript["id"]
        summary = self.summarize(text=transcript["text"], kind="all", model=model)
        label = title or _default_title(kind, mid)
        summary_uri, summary_md = self._store_summary(mid, summary, f"Resumo: {label}")
        # Reserved before ingest so the KB metadata names the file the original will actually become.
        archive_uri = (
            self.storage.available(make_uri(MEDIA_BUCKET, "archive/" + key.removeprefix("input/")))
            if key.startswith("input/")
            else None
        )

        ingested, warnings = [], []
        if ingest and not transcript["text"].strip():
            warnings.append("nenhuma fala detectada: nada foi enviado ao knowledge")
        elif ingest:
            meta = {
                "edge_id": mid,
                "media_uri": archive_uri or source,
                "media_kind": kind,
                "language": language,
                "duration_s": transcript["duration_s"],
                "srt_uri": transcript["srt_uri"],
                "transcript_uri": transcript["txt_uri"],
                "summary_uri": summary_uri,
                "summary_models": summary.get("meta", {}).get("models_used", []),
            }
            for doc, uri, doc_title, content in (
                ("transcript", transcript["txt_uri"], f"Transcrição: {label}", transcript["text"]),
                ("summary", summary_uri, f"Resumo: {label}", summary_md),
            ):
                res = self.knowledge.ingest(
                    tenant=tenant,
                    source_uri=uri,
                    title=doc_title,
                    content=content,
                    source_type=kind,
                    domain=domain,
                    metadata={**meta, "document": doc},
                )
                ingested.append({"document": doc, **res})

        archived = self._archive(source, archive_uri, warnings) if archive_uri else None
        log.info(
            "pipeline done",
            extra={"media_id": mid, "tenant": tenant, "ingested": len(ingested), "archived": bool(archived)},
        )
        return {
            "id": mid,
            "source": source,
            "archived_uri": archived,
            "media_kind": kind,
            "tenant": tenant,
            "domain": domain,
            "language": language,
            "duration_s": transcript["duration_s"],
            "segments": len(transcript["segments"]),
            "txt_uri": transcript["txt_uri"],
            "srt_uri": transcript["srt_uri"],
            "summary_uri": summary_uri,
            "summary": summary,
            "transcript_cached": transcript["cached"],
            "ingest": ingested,
            "warnings": warnings,
        }

    def _archive(self, source: str, archive_uri: str, warnings: list[str]) -> str | None:
        try:
            archived = self.storage.move(source, archive_uri)
        except FileNotFoundError:  # a concurrent job on the same source archived it first
            warnings.append("a fonte já não estava em input/ (outro job a arquivou); nada foi movido")
            return None
        if archived != archive_uri:
            log.warning("archive name taken meanwhile", extra={"reserved": archive_uri, "archived": archived})
        return archived

    def embed(self, texts: list[str]) -> dict:
        try:
            vectors = self.llm.embed(self.settings.embed_model, texts)
        except LLMError as exc:
            raise _llm_error(exc) from None
        return {"model": self.settings.embed_model, "dim": len(vectors[0]) if vectors else 0, "vectors": vectors}


def _default_title(kind: str | None, mid: str) -> str:
    label = "Vídeo" if kind == "video" else "Áudio"
    return f"{label} {datetime.now():%Y-%m-%d} ({mid[:12]})"
