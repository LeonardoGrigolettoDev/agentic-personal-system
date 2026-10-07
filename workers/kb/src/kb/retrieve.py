"""Hybrid retrieval (pgvector cosine + Portuguese full text) fused with Reciprocal Rank Fusion.

`search()` is the entry point meant to be reused by the Context Compiler / an MCP server: it takes
an open psycopg connection (the read-only 'aios_reader' role is enough) and an optional query
embedder, and always filters by tenant.
"""

from collections.abc import Hashable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Protocol

import psycopg
from pgvector import Vector

from kb.sources import DOMAINS, TENANTS

RRF_K = 60
DEFAULT_K = 8
CANDIDATE_FACTOR = 4


class QueryEmbedder(Protocol):
    def embed_query(self, query: str) -> list[float]: ...


@dataclass(frozen=True)
class Hit:
    chunk_id: int
    document_id: str
    tenant: str
    domain: str | None
    source_type: str
    source_uri: str
    title: str | None
    heading_path: str | None
    content: str
    snippet: str
    score: float
    vector_rank: int | None
    text_rank: int | None
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def rrf_fuse(rankings: Sequence[Sequence[Hashable]], k: int = RRF_K) -> list[tuple[Hashable, float]]:
    """Reciprocal Rank Fusion: score(d) = sum over rankings of 1 / (k + rank), rank starting at 1.

    Ties are broken by first appearance (earlier ranking lists first), so the result is deterministic.
    """
    scores: dict[Hashable, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    order = {item: i for i, item in enumerate(scores)}
    return sorted(scores.items(), key=lambda kv: (-kv[1], order[kv[0]]))


def tenant_scope(tenant: str, include_shared: bool = False) -> list[str]:
    if tenant not in TENANTS:
        raise ValueError(f"unknown tenant {tenant!r}")
    return [tenant, "shared"] if include_shared and tenant != "shared" else [tenant]


_FILTER = """d.tenant = ANY(%(tenants)s::tenant[]) AND d.status = 'active'
             AND (%(domain)s::domain IS NULL OR d.domain = %(domain)s::domain)"""

_VECTOR_SQL = f"""
SELECT c.id FROM chunks c JOIN documents d ON d.id = c.document_id
WHERE {_FILTER} AND c.embedding IS NOT NULL
ORDER BY c.embedding <=> %(qvec)s LIMIT %(limit)s"""

_TEXT_SQL = f"""
SELECT c.id FROM chunks c JOIN documents d ON d.id = c.document_id,
     websearch_to_tsquery('pt_unaccent', %(q)s) AS tsq
WHERE {_FILTER} AND c.tsv @@ tsq
ORDER BY ts_rank_cd(c.tsv, tsq) DESC, c.id LIMIT %(limit)s"""

_DETAILS_SQL = f"""
SELECT c.id, d.id::text, d.tenant::text, d.domain::text, d.source_type, d.source_uri, d.title,
       c.heading_path, c.content, c.metadata,
       ts_headline('pt_unaccent', c.content, websearch_to_tsquery('pt_unaccent', %(q)s),
                   'MaxWords=40, MinWords=15, MaxFragments=2, FragmentDelimiter=" … ", StartSel=«, StopSel=»')
FROM chunks c JOIN documents d ON d.id = c.document_id
WHERE c.id = ANY(%(ids)s) AND {_FILTER}"""


def _vector_ids(conn: psycopg.Connection, params: dict) -> list[int]:
    with conn.transaction():
        # Let HNSW keep scanning when the tenant filter discards candidates (pgvector >= 0.8).
        conn.execute("SELECT set_config('hnsw.ef_search', %s, true)", (str(min(1000, max(40, params["limit"]))),))
        try:
            with conn.transaction():
                conn.execute("SELECT set_config('hnsw.iterative_scan', 'relaxed_order', true)")
        except psycopg.Error:
            pass
        return [row[0] for row in conn.execute(_VECTOR_SQL, params).fetchall()]


def search(
    conn: psycopg.Connection,
    query: str,
    tenant: str,
    *,
    k: int = DEFAULT_K,
    domain: str | None = None,
    include_shared: bool = False,
    embedder: QueryEmbedder | None = None,
    query_vector: Sequence[float] | None = None,
) -> list[Hit]:
    """Top-k chunks for `query` within `tenant` (+ 'shared' if asked). Text-only if no vector is available."""
    query = query.strip()
    if not query:
        return []
    if domain is not None and domain not in DOMAINS:
        raise ValueError(f"unknown domain {domain!r}")
    if query_vector is None and embedder is not None:
        query_vector = embedder.embed_query(query)

    params = {"tenants": tenant_scope(tenant, include_shared), "domain": domain, "q": query, "limit": k * CANDIDATE_FACTOR}
    rankings: list[list[int]] = []
    vector_ids: list[int] = []
    if query_vector is not None:
        vector_ids = _vector_ids(conn, {**params, "qvec": Vector(list(query_vector))})
        rankings.append(vector_ids)
    text_ids = [row[0] for row in conn.execute(_TEXT_SQL, params).fetchall()]
    rankings.append(text_ids)

    fused = rrf_fuse(rankings)[:k]
    if not fused:
        return []
    rows = {row[0]: row for row in conn.execute(_DETAILS_SQL, {**params, "ids": [cid for cid, _ in fused]}).fetchall()}
    vector_rank = {cid: i for i, cid in enumerate(vector_ids, start=1)}
    text_rank = {cid: i for i, cid in enumerate(text_ids, start=1)}

    hits = []
    for cid, score in fused:
        if cid not in rows:
            continue
        _, doc_id, t, d, stype, uri, title, path, content, meta, headline = rows[cid]
        hits.append(
            Hit(
                chunk_id=cid, document_id=doc_id, tenant=t, domain=d, source_type=stype, source_uri=uri,
                title=title, heading_path=path, content=content,
                snippet=headline if cid in text_rank else _lead(content),
                score=round(score, 6), vector_rank=vector_rank.get(cid), text_rank=text_rank.get(cid),
                metadata=meta or {},
            )
        )
    return hits


def _lead(text: str, limit: int = 280) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit].rsplit(" ", 1)[0] + " …"
