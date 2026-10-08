"""Knowledge service: HTTP API + MCP (streamable HTTP) over the knowledge base.

Hermes reaches it as an MCP server (tools knowledge_search, compile_context, memory_save,
memory_search, ingest_note, project_list, project_upsert); other services use the JSON API. Every request except health checks
needs `Authorization: Bearer $KNOWLEDGE_API_KEY`. KNOWLEDGE_TENANTS caps which tenants this
instance may serve at all; per-session tenant isolation is enforced by Hermes' pre_tool_call hook.
"""

import contextlib
import hmac
import logging
import os
from typing import Any

import psycopg
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

from kb import context as ctxmod
from kb import memory, projects, retrieve
from kb.config import Settings, load_settings
from kb.embed import Embedder
from kb.ingest import ContentIn, ingest_content, validate_content
from kb.sources import DOMAINS, SOURCE_TYPES, TENANTS
from kb.store import PgStore

log = logging.getLogger("kb.service")


class State:
    settings: Settings
    pool: ConnectionPool
    embedder: Embedder
    api_key: str
    tenants: tuple[str, ...]


state = State()


def allowed(tenant: str) -> str:
    if tenant not in TENANTS:
        raise HTTPException(400, f"unknown tenant {tenant!r}")
    if tenant not in state.tenants:
        raise HTTPException(403, f"tenant {tenant!r} is not served by this instance")
    return tenant


def scope_for(tenant: str, include_shared: bool) -> list[str]:
    return [t for t in retrieve.tenant_scope(allowed(tenant), include_shared) if t in state.tenants]


@contextlib.contextmanager
def conn():
    with state.pool.connection() as c:
        yield c


# ---------------------------------------------------------------- models
class SearchIn(BaseModel):
    tenant: str
    query: str = Field(min_length=1, max_length=2000)
    domain: str | None = None
    k: int = Field(8, ge=1, le=50)
    include_shared: bool = False


class CompileIn(BaseModel):
    task: str = Field(min_length=1, max_length=20000)
    tenant: str
    domain: str | None = None
    project: str | None = None
    budget_tokens: int = Field(3000, ge=200, le=200000)
    include_shared: bool = True
    format: str = Field("json", pattern="^(json|markdown)$")


class MemoryInModel(BaseModel):
    tenant: str
    scope: str
    kind: str
    content: str = Field(min_length=1, max_length=memory.MAX_CONTENT_CHARS)
    lifecycle: str = "important"
    domain: str | None = None
    project: str | None = None
    importance: float = Field(0.5, ge=0, le=1)
    source_run_id: str | None = None


class MemorySearchIn(BaseModel):
    tenant: str
    query: str = Field(min_length=1, max_length=2000)
    scope: str | None = None
    domain: str | None = None
    project: str | None = None
    kind: str | None = None
    k: int = Field(10, ge=1, le=100)
    include_shared: bool = True


class ProjectModel(BaseModel):
    tenant: str
    slug: str
    name: str
    domain: str | None = None  # None: engineering on create, unchanged on update
    description: str | None = None
    status: str | None = None  # None: active on create, unchanged on update
    repository: str | None = None


class IngestIn(BaseModel):
    tenant: str
    source_uri: str = Field(min_length=3, max_length=2000)
    title: str = Field("", max_length=500)
    content: str = Field(min_length=1, max_length=5_000_000)
    source_type: str = "agent"
    domain: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------- operations (shared by HTTP + MCP)
def op_search(p: SearchIn) -> dict:
    if p.domain is not None and p.domain not in DOMAINS:
        raise HTTPException(400, f"unknown domain {p.domain!r}")
    tenants = scope_for(p.tenant, p.include_shared)
    qvec = _try_embed(p.query)
    with conn() as c:
        hits = retrieve.search(c, p.query, p.tenant, k=p.k, domain=p.domain,
                               include_shared=p.include_shared and "shared" in tenants, query_vector=qvec)
    return {"mode": "hybrid" if qvec is not None else "text",
            "results": [{k: v for k, v in h.to_dict().items() if k != "content"} | {"content": h.content} for h in hits]}


def op_compile(p: CompileIn) -> dict:
    allowed(p.tenant)
    req = ctxmod.ContextRequest(task=p.task, tenant=p.tenant, budget_tokens=p.budget_tokens, domain=p.domain,
                                project=p.project, include_shared=p.include_shared and "shared" in state.tenants)
    try:
        with conn() as c:
            out = ctxmod.compile_context(c, req, state.embedder)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if p.format == "markdown":
        out["markdown"] = ctxmod.render(out)
    return out


def op_memory_save(p: MemoryInModel) -> dict:
    allowed(p.tenant)
    m = memory.MemoryIn(tenant=p.tenant, scope=p.scope, kind=p.kind, content=p.content, lifecycle=p.lifecycle,
                        domain=p.domain, project=p.project, importance=p.importance, source_run_id=p.source_run_id)
    try:
        m.validate()
        vec = state.embedder.embed_documents([p.content])[0]
        with conn() as c:
            res = memory.save(c, m, vec, state.settings.embed_model_name)
    except memory.MemoryInputError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"id": res.id, "action": res.action, "superseded_id": res.superseded_id}


def op_project_upsert(p: ProjectModel) -> dict:
    allowed(p.tenant)
    spec = projects.ProjectIn(**p.model_dump())
    try:
        spec.validate()
        with conn() as c:
            project, created = projects.upsert(c, spec)
    except projects.ProjectConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except projects.ProjectInputError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"project": project.to_dict(), "created": created}


def op_project_list(tenant: str, status: str | None = None) -> dict:
    tenants = [allowed(tenant)]
    try:
        with conn() as c:
            items = projects.list_projects(c, tenants, status=status)
    except projects.ProjectInputError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"results": [p.to_dict() for p in items]}


def op_memory_search(p: MemorySearchIn) -> dict:
    tenants = scope_for(p.tenant, p.include_shared)
    vec = state.embedder.embed_query(p.query)
    with conn() as c:
        items = memory.search(c, tenants, vec, scope=p.scope, domain=p.domain, project=p.project, kind=p.kind, k=p.k)
    return {"results": [m.to_dict() for m in items]}


def op_ingest(p: IngestIn) -> dict:
    allowed(p.tenant)
    if p.source_type not in SOURCE_TYPES:
        raise HTTPException(400, f"unknown source_type {p.source_type!r}")
    item = ContentIn(tenant=p.tenant, source_uri=p.source_uri, title=p.title, content=p.content,
                     source_type=p.source_type, domain=p.domain, metadata=p.metadata)
    try:
        validate_content(item)
        with conn() as c:
            action, uri = ingest_content(item, PgStore(c, state.settings.embed_model_name), state.embedder)
            PgStore(c, state.settings.embed_model_name).record_ingest(p.tenant, {"api": uri, action: 1})
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"action": action, "source_uri": uri}


def _try_embed(query: str):
    try:
        return state.embedder.embed_query(query)
    except Exception as exc:
        log.warning("query embedding failed, text-only search: %s", exc)
        return None


# ---------------------------------------------------------------- MCP
def _tool(fn, payload):
    """Run an operation for an MCP tool: client errors become tool errors the model can read."""
    try:
        return fn(payload)
    except HTTPException as exc:
        raise ToolError(str(exc.detail)) from None


mcp = MCPServer(
    "aios-knowledge",
    instructions=(
        "Base de conhecimento pessoal (documentos, notas, transcrições) e memória em camadas "
        "(global/domínio/projeto). Sempre passe o tenant da tarefa atual (nitro=trabalho, pessoal=vida pessoal, "
        "shared=global). Busque antes de perguntar ao usuário; salve só o que tem valor futuro."
    ),
)


@mcp.tool()
def knowledge_search(tenant: str, query: str, domain: str | None = None, k: int = 8, include_shared: bool = False) -> dict:
    """Hybrid (semantic + full-text pt-BR) search over the knowledge base of one tenant."""
    return _tool(op_search, SearchIn(tenant=tenant, query=query, domain=domain, k=k, include_shared=include_shared))


@mcp.tool()
def compile_context(task: str, tenant: str, domain: str | None = None, project: str | None = None,
                    budget_tokens: int = 3000) -> dict:
    """Compile a compact task context (rules, decisions, memories, documents) within a token budget."""
    return _tool(op_compile, CompileIn(task=task, tenant=tenant, domain=domain, project=project,
                                budget_tokens=budget_tokens, format="markdown"))


@mcp.tool()
def memory_save(tenant: str, scope: str, kind: str, content: str, lifecycle: str = "important",
                domain: str | None = None, project: str | None = None, importance: float = 0.5) -> dict:
    """Save a durable memory. scope: global|domain|project. kind: preference|working_style|communication|rule|
    fact|decision|architecture|roadmap|constraint|issue|episode|procedure. lifecycle: temporary|important|persistent.
    Near-duplicates supersede older memories automatically."""
    return _tool(op_memory_save, MemoryInModel(tenant=tenant, scope=scope, kind=kind, content=content, lifecycle=lifecycle,
                                        domain=domain, project=project, importance=importance))


@mcp.tool()
def memory_search(tenant: str, query: str, scope: str | None = None, domain: str | None = None,
                  project: str | None = None, kind: str | None = None, k: int = 10) -> dict:
    """Semantic search over live memories of a tenant (plus shared)."""
    return _tool(op_memory_search, MemorySearchIn(tenant=tenant, query=query, scope=scope, domain=domain,
                                           project=project, kind=kind, k=k))


@mcp.tool()
def ingest_note(tenant: str, title: str, content: str, domain: str | None = None, source_uri: str | None = None) -> dict:
    """Store a note/summary produced during work as a knowledge-base document (markdown)."""
    import hashlib

    uri = source_uri or f"agent://notes/{hashlib.sha256((title + content).encode()).hexdigest()[:16]}"
    return _tool(op_ingest, IngestIn(tenant=tenant, source_uri=uri, title=title, content=content, domain=domain))


@mcp.tool()
def project_list(tenant: str, status: str | None = None) -> dict:
    """Projects of a tenant (slug, name, domain, status, repository). Use the slug in memory_save/compile_context."""
    try:
        return op_project_list(tenant, status)
    except HTTPException as exc:
        raise ToolError(str(exc.detail)) from None


@mcp.tool()
def project_upsert(tenant: str, slug: str, name: str, domain: str | None = None, repository: str | None = None,
                   description: str | None = None, status: str | None = None) -> dict:
    """Register or update a project so project-scoped memories can point at it. slug: a-z0-9-.
    repository: git remote (https://, ssh://, git@host:) or storage://. status: active|paused|archived.
    Omitted fields keep their stored values on update (new projects: domain engineering, status active)."""
    return _tool(op_project_upsert, ProjectModel(tenant=tenant, slug=slug, name=name, domain=domain,
                                                 repository=repository, description=description, status=status))


# ---------------------------------------------------------------- HTTP app
class BearerAuth(BaseHTTPMiddleware):
    OPEN = {"/healthz", "/readyz"}

    async def dispatch(self, request: Request, call_next):
        if request.url.path not in self.OPEN:
            token = request.headers.get("authorization", "").removeprefix("Bearer ")
            if not state.api_key or not hmac.compare_digest(token.encode(), state.api_key.encode()):
                return JSONResponse({"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
        return await call_next(request)


def create_app(settings: Settings | None = None, *, pool: ConnectionPool | None = None,
               embedder: Embedder | None = None, api_key: str | None = None) -> FastAPI:
    mcp_app = mcp.streamable_http_app(streamable_http_path="/", stateless_http=True, json_response=True,
                                      host="0.0.0.0")

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        state.settings = settings or load_settings()
        state.api_key = api_key if api_key is not None else os.environ.get("KNOWLEDGE_API_KEY", "")
        if not state.api_key:
            raise RuntimeError("KNOWLEDGE_API_KEY is required")
        state.tenants = tuple(t.strip() for t in os.environ.get("KNOWLEDGE_TENANTS", ",".join(TENANTS)).split(",")
                              if t.strip() in TENANTS)
        state.embedder = embedder or Embedder.from_settings(state.settings, max_retries=2)
        state.pool = pool or ConnectionPool(
            state.settings.database_url, min_size=1, max_size=int(os.environ.get("KB_POOL_MAX", "4")),
            open=False, configure=_configure, kwargs={"autocommit": True, "application_name": "aios-knowledge"},
        )
        if pool is None:
            state.pool.open(wait=False)
        async with mcp.session_manager.run():
            yield
        if pool is None:
            state.pool.close()

    app = FastAPI(title="AIOS knowledge", lifespan=lifespan)
    app.add_middleware(BearerAuth)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz():
        try:
            with state.pool.connection(timeout=2) as c:
                c.execute("SELECT 1 FROM schema_migrations WHERE version = '004_knowledge'").fetchone()
        except Exception as exc:
            return JSONResponse({"status": "not ready", "error": type(exc).__name__}, status_code=503)
        return {"status": "ready"}

    @app.post("/v1/search")
    def http_search(p: SearchIn):
        return op_search(p)

    @app.post("/v1/context/compile")
    def http_compile(p: CompileIn):
        return op_compile(p)

    @app.post("/v1/memories")
    def http_memory_save(p: MemoryInModel):
        return op_memory_save(p)

    @app.post("/v1/memories/search")
    def http_memory_search(p: MemorySearchIn):
        return op_memory_search(p)

    @app.post("/v1/ingest")
    def http_ingest(p: IngestIn):
        return op_ingest(p)

    @app.get("/v1/memories")
    def list_memories(tenant: str, scope: str | None = None, domain: str | None = None, project: str | None = None,
                      kind: str | None = None, include_shared: bool = True, limit: int = 100):
        with conn() as c:
            items = memory.list_memories(c, scope_for(tenant, include_shared), scope=scope, domain=domain,
                                         project=project, kind=kind, limit=limit)
        return {"results": [m.to_dict() for m in items]}

    @app.get("/v1/projects")
    def http_project_list(tenant: str, status: str | None = None):
        return op_project_list(tenant, status)

    @app.post("/v1/projects")
    def http_project_upsert(p: ProjectModel):
        return op_project_upsert(p)

    @app.post("/v1/memories/{memory_id}/deprecate")
    def deprecate(memory_id: str, tenant: str):
        with conn() as c:
            ok = memory.deprecate(c, memory_id, [allowed(tenant)])
        if not ok:
            raise HTTPException(404, "memory not found or already deprecated")
        return {"status": "deprecated"}

    @app.post("/v1/maintain")
    def maintain():
        with conn() as c:
            return memory.maintain(c)

    @app.get("/v1/stats")
    def stats():
        with conn() as c:
            return PgStore(c, state.settings.embed_model_name).stats()

    @app.exception_handler(psycopg.OperationalError)
    async def db_down(_: Request, exc: psycopg.OperationalError):
        return JSONResponse({"error": "database unavailable"}, status_code=503)

    app.mount("/mcp", mcp_app)
    return app


def _configure(c: psycopg.Connection) -> None:
    from pgvector.psycopg import register_vector

    register_vector(c)
