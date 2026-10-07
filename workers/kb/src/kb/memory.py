"""Layered memory (docs/ARCHITECTURE.md §11): global / domain / project, with an episodic lifecycle.

Only things with future value are kept. A new memory that is a near-duplicate of a live one in the
same scope supersedes it (history kept via superseded_by), instead of piling up. 'temporary'
memories expire; `maintain()` deprecates expired and superseded ones.
"""

from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime

import psycopg
from pgvector import Vector
from psycopg.types.json import Jsonb

from kb.sources import DOMAINS, TENANTS

SCOPES = ("global", "domain", "project")
KINDS = (
    "preference", "working_style", "communication", "rule", "fact", "decision", "architecture",
    "roadmap", "constraint", "issue", "episode", "procedure",
)
LIFECYCLES = ("temporary", "important", "persistent", "deprecated")
SUPERSEDE_SIMILARITY = 0.92  # cosine similarity at/above which a new memory replaces an old one
TEMPORARY_TTL_DAYS = 14
MAX_CONTENT_CHARS = 4000


class MemoryInputError(ValueError):
    pass


@dataclass(frozen=True)
class MemoryIn:
    tenant: str
    scope: str
    kind: str
    content: str
    lifecycle: str = "important"
    domain: str | None = None
    project: str | None = None  # project slug
    importance: float = 0.5
    source_run_id: str | None = None
    source_document_id: str | None = None
    expires_at: datetime | None = None

    def validate(self) -> None:
        if self.tenant not in TENANTS:
            raise MemoryInputError(f"unknown tenant {self.tenant!r}")
        if self.scope not in SCOPES:
            raise MemoryInputError(f"scope must be one of {SCOPES}")
        if self.kind not in KINDS:
            raise MemoryInputError(f"kind must be one of {KINDS}")
        if self.lifecycle not in LIFECYCLES or self.lifecycle == "deprecated":
            raise MemoryInputError("lifecycle must be temporary|important|persistent")
        if self.domain is not None and self.domain not in DOMAINS:
            raise MemoryInputError(f"unknown domain {self.domain!r}")
        if self.scope == "domain" and not self.domain:
            raise MemoryInputError("scope 'domain' requires domain")
        if self.scope == "project" and not self.project:
            raise MemoryInputError("scope 'project' requires project")
        if not self.content.strip():
            raise MemoryInputError("content is empty")
        if len(self.content) > MAX_CONTENT_CHARS:
            raise MemoryInputError(f"content longer than {MAX_CONTENT_CHARS} chars: store it as a document instead")
        if not 0 <= self.importance <= 1:
            raise MemoryInputError("importance must be within [0,1]")


@dataclass
class Memory:
    id: str
    tenant: str
    scope: str
    domain: str | None
    project: str | None
    kind: str
    lifecycle: str
    content: str
    importance: float
    created_at: str
    similarity: float | None = None
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class SaveResult:
    id: str
    action: str  # created | superseded | duplicate
    superseded_id: str | None = None


def project_id(conn: psycopg.Connection, slug: str | None, tenant: str) -> str | None:
    if slug is None:
        return None
    row = conn.execute("SELECT id::text, tenant::text FROM projects WHERE slug = %s", (slug,)).fetchone()
    if row is None:
        raise MemoryInputError(f"unknown project {slug!r}")
    if row[1] not in (tenant, "shared"):
        raise MemoryInputError(f"project {slug!r} belongs to tenant {row[1]!r}")
    return row[0]


def _live(alias: str = "") -> str:
    a = f"{alias}." if alias else ""
    return f"{a}superseded_by IS NULL AND {a}lifecycle <> 'deprecated' AND ({a}expires_at IS NULL OR {a}expires_at > now())"


def save(conn: psycopg.Connection, m: MemoryIn, embedding: Sequence[float], embedding_model: str) -> SaveResult:
    m.validate()
    pid = project_id(conn, m.project, m.tenant)
    vec = Vector(list(embedding))
    with conn.transaction():
        near = conn.execute(
            f"""SELECT id::text, content, 1 - (embedding <=> %s) AS sim FROM memories
                WHERE tenant = %s::tenant AND scope = %s AND domain IS NOT DISTINCT FROM %s::domain
                  AND project_id IS NOT DISTINCT FROM %s::uuid AND kind = %s AND embedding IS NOT NULL AND {_live()}
                ORDER BY embedding <=> %s LIMIT 1""",
            (vec, m.tenant, m.scope, m.domain, pid, m.kind, vec),
        ).fetchone()
        if near and near[1].strip() == m.content.strip():
            return SaveResult(near[0], "duplicate")
        expires = m.expires_at
        if expires is None and m.lifecycle == "temporary":
            expires = conn.execute(f"SELECT now() + interval '{TEMPORARY_TTL_DAYS} days'").fetchone()[0]
        (new_id,) = conn.execute(
            """INSERT INTO memories (tenant, scope, domain, project_id, kind, lifecycle, content, embedding,
                                     embedding_model, importance, source_run_id, source_document_id, expires_at)
               VALUES (%s::tenant, %s, %s::domain, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id::text""",
            (m.tenant, m.scope, m.domain, pid, m.kind, m.lifecycle, m.content.strip(), vec, embedding_model,
             m.importance, m.source_run_id, m.source_document_id, expires),
        ).fetchone()
        if near and near[2] >= SUPERSEDE_SIMILARITY:
            conn.execute("UPDATE memories SET superseded_by = %s WHERE id = %s", (new_id, near[0]))
            return SaveResult(new_id, "superseded", near[0])
    return SaveResult(new_id, "created")


_SELECT = """SELECT m.id::text, m.tenant::text, m.scope, m.domain::text, p.slug, m.kind, m.lifecycle, m.content,
                    m.importance, m.created_at::text"""


def _filters(tenants: Sequence[str], scope: str | None, domain: str | None, project: str | None, kind: str | None):
    where = ["m.tenant = ANY(%(tenants)s::tenant[])", _live("m")]
    params: dict = {"tenants": list(tenants)}
    for col, val in (("m.scope", scope), ("m.domain", domain), ("p.slug", project), ("m.kind", kind)):
        if val is not None:
            key = col.split(".")[1]
            where.append(f"{col} = %({key})s" + ("::domain" if key == "domain" else ""))
            params[key] = val
    return " AND ".join(where), params


def list_memories(conn, tenants: Sequence[str], *, scope=None, domain=None, project=None, kind=None, limit=100) -> list[Memory]:
    where, params = _filters(tenants, scope, domain, project, kind)
    rows = conn.execute(
        f"""{_SELECT} FROM memories m LEFT JOIN projects p ON p.id = m.project_id
            WHERE {where} ORDER BY m.importance DESC, m.created_at DESC LIMIT %(limit)s""",
        {**params, "limit": min(limit, 500)},
    ).fetchall()
    return [Memory(*r) for r in rows]


def search(conn, tenants: Sequence[str], embedding: Sequence[float], *, scope=None, domain=None, project=None,
           kind=None, k: int = 10, min_similarity: float = 0.0) -> list[Memory]:
    where, params = _filters(tenants, scope, domain, project, kind)
    rows = conn.execute(
        f"""{_SELECT}, 1 - (m.embedding <=> %(vec)s) AS sim FROM memories m LEFT JOIN projects p ON p.id = m.project_id
            WHERE {where} AND m.embedding IS NOT NULL ORDER BY m.embedding <=> %(vec)s LIMIT %(k)s""",
        {**params, "vec": Vector(list(embedding)), "k": min(k, 100)},
    ).fetchall()
    return [Memory(*r[:10], similarity=round(float(r[10]), 4)) for r in rows if r[10] >= min_similarity]


def deprecate(conn, memory_id: str, tenants: Sequence[str]) -> bool:
    cur = conn.execute(
        "UPDATE memories SET lifecycle = 'deprecated' WHERE id = %s AND tenant = ANY(%s::tenant[]) AND lifecycle <> 'deprecated'",
        (memory_id, list(tenants)),
    )
    return cur.rowcount > 0


def maintain(conn) -> dict:
    """Expire temporary memories, deprecate superseded ones, drop chunks of deleted documents."""
    with conn.transaction():
        expired = conn.execute(
            "UPDATE memories SET lifecycle = 'deprecated' WHERE lifecycle <> 'deprecated' AND expires_at <= now()"
        ).rowcount
        superseded = conn.execute(
            "UPDATE memories SET lifecycle = 'deprecated' WHERE lifecycle <> 'deprecated' AND superseded_by IS NOT NULL"
        ).rowcount
        orphan_chunks = conn.execute(
            "DELETE FROM chunks c USING documents d WHERE d.id = c.document_id AND d.status <> 'active'"
        ).rowcount
        conn.execute(
            "INSERT INTO events (source, type, payload) VALUES ('kb', 'kb.maintain', %s)",
            (Jsonb({"expired": expired, "superseded": superseded, "orphan_chunks": orphan_chunks}),),
        )
    return {"expired": expired, "superseded": superseded, "orphan_chunks": orphan_chunks}
