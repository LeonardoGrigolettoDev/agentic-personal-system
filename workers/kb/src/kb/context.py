"""Context Compiler (docs/ARCHITECTURE.md §9): large memory in, small relevant Task Context out.

Knowledge Base -> retriever -> candidates -> relevance filter -> greedy fill under a token budget.
Priority when the budget is tight: constraints/rules > decisions > project state > memories > documents.
Output order is deterministic so the dynamic part of the prompt stays as cache-friendly as possible (§16).
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass

import psycopg

from kb import memory, retrieve
from kb.chunker import estimate_tokens
from kb.retrieve import QueryEmbedder
from kb.sources import DOMAINS

log = logging.getLogger(__name__)

CONSTRAINT_KINDS = {"rule", "constraint"}
DECISION_KINDS = {"decision", "architecture"}
MIN_MEMORY_SIMILARITY = 0.35
DOC_K = 8


@dataclass(frozen=True)
class ContextRequest:
    task: str
    tenant: str
    budget_tokens: int = 3000
    domain: str | None = None
    project: str | None = None
    include_shared: bool = True

    def validate(self) -> None:
        if not self.task.strip():
            raise ValueError("task is empty")
        if self.domain is not None and self.domain not in DOMAINS:
            raise ValueError(f"unknown domain {self.domain!r}")
        if not 200 <= self.budget_tokens <= 200_000:
            raise ValueError("budget_tokens must be within [200, 200000]")


def _project(conn, slug: str | None, tenants: Sequence[str]) -> dict | None:
    if not slug:
        return None
    row = conn.execute(
        """SELECT slug, name, tenant::text, domain::text, description, status, repository, metadata
           FROM projects WHERE slug = %s AND tenant = ANY(%s::tenant[])""",
        (slug, list(tenants)),
    ).fetchone()
    if row is None:
        return None
    slug, name, tenant, domain, desc, status, repo, meta = row
    return {"slug": slug, "name": name, "tenant": tenant, "domain": domain, "description": desc,
            "status": status, "repository": repo, "current_state": (meta or {}).get("current_state")}


def _memory_candidates(conn, req: ContextRequest, tenants: list[str], qvec) -> list[memory.Memory]:
    found: dict[str, memory.Memory] = {}

    def add(items):
        for m in items:
            prev = found.get(m.id)
            if prev is None or (m.similarity or 0) > (prev.similarity or 0):
                found[m.id] = m

    # standing rules/preferences apply regardless of similarity
    add(memory.list_memories(conn, tenants, scope="global", kind="rule", limit=20))
    add(memory.list_memories(conn, tenants, scope="global", kind="constraint", limit=20))
    if req.project:
        add(memory.list_memories(conn, tenants, scope="project", project=req.project, kind="constraint", limit=20))
    if qvec is None:
        add(memory.list_memories(conn, tenants, domain=req.domain, limit=30))
        return list(found.values())
    add(memory.search(conn, tenants, qvec, k=20, min_similarity=MIN_MEMORY_SIMILARITY))
    if req.domain:
        add(memory.search(conn, tenants, qvec, domain=req.domain, k=10, min_similarity=MIN_MEMORY_SIMILARITY))
    if req.project:
        add(memory.search(conn, tenants, qvec, project=req.project, k=15, min_similarity=MIN_MEMORY_SIMILARITY))
    return list(found.values())


def _rank(m: memory.Memory) -> float:
    return (m.similarity if m.similarity is not None else 0.5) * 0.7 + m.importance * 0.3


class _Budget:
    def __init__(self, total: int) -> None:
        self.left = total
        self.used = 0
        self.dropped = 0

    def take(self, text: str) -> bool:
        cost = estimate_tokens(text) + 4  # list/JSON overhead
        if cost > self.left:
            self.dropped += 1
            return False
        self.left -= cost
        self.used += cost
        return True


def compile_context(conn: psycopg.Connection, req: ContextRequest, embedder: QueryEmbedder | None) -> dict:
    req.validate()
    tenants = retrieve.tenant_scope(req.tenant, req.include_shared)
    qvec = None
    if embedder is not None:
        try:
            qvec = embedder.embed_query(req.task)
        except Exception as exc:  # embeddings down: degrade to text search + importance
            log.warning("context: query embedding failed, degrading to full text: %s", exc)

    budget = _Budget(req.budget_tokens)
    budget.take(req.task)
    project = _project(conn, req.project, tenants)
    if project:
        budget.take(" ".join(str(v) for v in project.values() if v))

    memories = sorted(_memory_candidates(conn, req, tenants, qvec), key=lambda m: (-_rank(m), m.id))
    constraints, decisions, others = [], [], []
    for m in memories:
        (constraints if m.kind in CONSTRAINT_KINDS else decisions if m.kind in DECISION_KINDS else others).append(m)

    def fill(items: list[memory.Memory]) -> list[dict]:
        out = []
        for m in items:
            if budget.take(m.content):
                out.append({"id": m.id, "kind": m.kind, "scope": m.scope, "content": m.content,
                            "importance": m.importance, "similarity": m.similarity})
        return out

    picked_constraints = fill(constraints)
    picked_decisions = fill(decisions)
    picked_memories = fill(others)

    documents = []
    hits = retrieve.search(conn, req.task, req.tenant, k=DOC_K, domain=req.domain,
                           include_shared=req.include_shared, query_vector=qvec)
    for h in hits:
        text = h.content if budget.left > estimate_tokens(h.content) + 4 else h.snippet
        if budget.take(text):
            documents.append({"title": h.title, "source_uri": h.source_uri, "heading_path": h.heading_path,
                              "content": text, "score": h.score})

    return {
        "task": req.task,
        "tenant": req.tenant,
        "domain": req.domain,
        "project": project,
        "current_state": project["current_state"] if project else None,
        "constraints": picked_constraints,
        "relevant_decisions": picked_decisions,
        "relevant_memories": picked_memories,
        "relevant_documents": documents,
        "token_estimate": budget.used,
        "budget_tokens": req.budget_tokens,
        "dropped_items": budget.dropped,
        "retrieval_mode": "hybrid" if qvec is not None else "text",
    }


def render(ctx: dict) -> str:
    """Markdown rendering of a compiled context, for prompts (stable section order)."""
    lines = [f"# Contexto da tarefa ({ctx['tenant']}{' / ' + ctx['domain'] if ctx.get('domain') else ''})"]
    if p := ctx.get("project"):
        lines.append(f"\n## Projeto: {p['name']} ({p['slug']}, {p['status']})")
        if p.get("description"):
            lines.append(p["description"])
        if p.get("current_state"):
            lines.append(f"Estado atual: {p['current_state']}")
    for title, key in (("Restrições e regras", "constraints"), ("Decisões relevantes", "relevant_decisions"),
                       ("Memórias relevantes", "relevant_memories")):
        if ctx.get(key):
            lines.append(f"\n## {title}")
            lines.extend(f"- [{m['kind']}] {m['content']}" for m in ctx[key])
    if ctx.get("relevant_documents"):
        lines.append("\n## Documentos")
        for d in ctx["relevant_documents"]:
            where = f" § {d['heading_path']}" if d.get("heading_path") else ""
            lines.append(f"\n### {d['title'] or d['source_uri']}{where}\n_fonte: {d['source_uri']}_\n\n{d['content']}")
    return "\n".join(lines)
