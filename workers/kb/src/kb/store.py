"""Postgres persistence for documents/chunks (role 'aios', which owns the tables)."""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import psycopg
from pgvector import Vector
from pgvector.psycopg import register_vector
from psycopg.types.json import Jsonb

from kb.chunker import Chunk


def connect(database_url: str, **kwargs) -> psycopg.Connection:
    conn = psycopg.connect(database_url, autocommit=True, application_name="aios-kb", **kwargs)
    register_vector(conn)
    return conn


@dataclass(frozen=True)
class DocState:
    id: str
    content_hash: str
    status: str
    stale_embeddings: bool  # some chunk was embedded with a different model


@dataclass(frozen=True)
class DocumentRow:
    tenant: str
    domain: str | None
    source_type: str
    source_uri: str
    title: str
    mime_type: str
    content_hash: str
    metadata: dict


class PgStore:
    def __init__(self, conn: psycopg.Connection, embedding_model: str) -> None:
        self.conn = conn
        self.embedding_model = embedding_model

    def get_state(self, tenant: str, source_uri: str) -> DocState | None:
        row = self.conn.execute(
            """SELECT d.id::text, d.content_hash, d.status,
                      EXISTS (SELECT 1 FROM chunks c WHERE c.document_id = d.id AND c.embedding_model <> %s)
               FROM documents d WHERE d.tenant = %s::tenant AND d.source_uri = %s""",
            (self.embedding_model, tenant, source_uri),
        ).fetchone()
        return DocState(*row) if row else None

    def write_document(self, doc: DocumentRow, chunks: Sequence[Chunk], vectors: Sequence[Sequence[float]]) -> str:
        """Upsert the document and replace all of its chunks, atomically."""
        with self.conn.transaction():
            (doc_id,) = self.conn.execute(
                """INSERT INTO documents (tenant, domain, source_type, source_uri, title, mime_type,
                                          content_hash, status, metadata)
                   VALUES (%s::tenant, %s::domain, %s, %s, %s, %s, %s, 'active', %s)
                   ON CONFLICT (tenant, source_uri) DO UPDATE SET
                     domain = coalesce(EXCLUDED.domain, documents.domain),
                     source_type = EXCLUDED.source_type, title = EXCLUDED.title,
                     mime_type = EXCLUDED.mime_type, content_hash = EXCLUDED.content_hash,
                     status = 'active', metadata = EXCLUDED.metadata
                   RETURNING id::text""",
                (doc.tenant, doc.domain, doc.source_type, doc.source_uri, doc.title, doc.mime_type,
                 doc.content_hash, Jsonb(doc.metadata)),
            ).fetchone()
            self.conn.execute("DELETE FROM chunks WHERE document_id = %s", (doc_id,))
            with self.conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO chunks (document_id, chunk_index, heading_path, content, token_count,
                                           embedding, embedding_model, metadata)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    [
                        (doc_id, c.index, c.heading_path, c.content, c.token_count, Vector(list(v)),
                         self.embedding_model, Jsonb(c.metadata))
                        for c, v in zip(chunks, vectors, strict=True)
                    ],
                )
        return doc_id

    def update_metadata(self, doc: DocumentRow) -> bool:
        """Refresh title/domain/metadata of an unchanged document. Returns True if anything changed."""
        cur = self.conn.execute(
            """UPDATE documents SET domain = coalesce(%s::domain, domain), source_type = %s, title = %s,
                                    mime_type = %s, metadata = %s
               WHERE tenant = %s::tenant AND source_uri = %s
                 AND (domain, source_type, title, mime_type, metadata)
                     IS DISTINCT FROM (coalesce(%s::domain, domain), %s, %s, %s, %s::jsonb)""",
            (doc.domain, doc.source_type, doc.title, doc.mime_type, Jsonb(doc.metadata), doc.tenant,
             doc.source_uri, doc.domain, doc.source_type, doc.title, doc.mime_type, Jsonb(doc.metadata)),
        )
        return cur.rowcount > 0

    def prune(self, tenant: str, prefixes: Iterable[str], keep: set[str]) -> list[str]:
        """Mark active documents under `prefixes` that are not in `keep` as deleted and drop their chunks."""
        pruned: list[str] = []
        for prefix in prefixes:
            rows = self.conn.execute(
                """SELECT id, source_uri FROM documents
                   WHERE tenant = %s::tenant AND status <> 'deleted' AND left(source_uri, length(%s)) = %s""",
                (tenant, prefix, prefix),
            ).fetchall()
            for doc_id, uri in rows:
                if uri in keep:
                    continue
                with self.conn.transaction():
                    self.conn.execute("DELETE FROM chunks WHERE document_id = %s", (doc_id,))
                    self.conn.execute("UPDATE documents SET status = 'deleted' WHERE id = %s", (doc_id,))
                pruned.append(uri)
        return pruned

    def record_ingest(self, tenant: str, payload: dict) -> None:
        self.conn.execute(
            "INSERT INTO events (source, type, tenant, payload) VALUES ('kb', 'kb.ingest', %s::tenant, %s)",
            (tenant, Jsonb(payload)),
        )

    def stats(self) -> dict:
        groups = self.conn.execute(
            """SELECT d.tenant::text, d.domain::text, d.source_type,
                      count(*) FILTER (WHERE d.status = 'active'),
                      count(*) FILTER (WHERE d.status <> 'active'),
                      coalesce(sum(c.n), 0)::bigint,
                      max(d.updated_at)
               FROM documents d
               LEFT JOIN (SELECT document_id, count(*) AS n FROM chunks GROUP BY 1) c ON c.document_id = d.id
               GROUP BY 1, 2, 3 ORDER BY 1, 2 NULLS FIRST, 3"""
        ).fetchall()
        ingests = self.conn.execute(
            """SELECT tenant::text, max(occurred_at) FROM events
               WHERE source = 'kb' AND type = 'kb.ingest' GROUP BY 1 ORDER BY 1"""
        ).fetchall()
        return {
            "groups": [
                {
                    "tenant": t, "domain": d, "source_type": s, "documents": docs, "inactive_documents": inactive,
                    "chunks": chunks, "last_updated": updated.isoformat() if updated else None,
                }
                for t, d, s, docs, inactive, chunks, updated in groups
            ],
            "last_ingest": {t: ts.isoformat() for t, ts in ingests},
        }
