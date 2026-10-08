"""agent-edge HTTP API (CONTRACTS §5): media upload, audio extraction, transcription, summaries, pipeline.

Every route except /healthz and /readyz needs a bearer key:
  EDGE_API_KEY       admin (operator, make targets): every route and tenant
  EDGE_TENANT_KEYS   one key per tenant, for agent sessions: only /upload, /pipeline into that tenant from
                     media/input/, and its own jobs (a session cannot read or write another tenant's media)
Heavy operations run on the in-process job queue; pass {"async": true} to get a job id instead of waiting.
Run a single process (no --workers): jobs live in memory. Retention runs at startup and every
EDGE_MAINTAIN_INTERVAL_HOURS.
"""

import asyncio
import contextlib
import hmac
import logging
import os
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.types import ASGIApp, Receive, Scope, Send

from edge import retention
from edge.config import ConfigError, Settings
from edge.errors import BadRequest, EdgeError, Forbidden, NotFound
from edge.jobs import JobManager
from edge.logs import setup_logging
from edge.pipeline import Edge
from edge.storage import LocalStorage, StorageError
from edge.upload import receive_upload

log = logging.getLogger("edge.app")

SOURCE = Field(min_length=12, max_length=1024, pattern=r"^storage://")
LANGUAGE = Field("pt", pattern=r"^(auto|[a-z]{2,3})$")
MODEL = Field(None, pattern=r"^[A-Za-z0-9._:/-]{1,128}$")
Kind = Literal["summary", "notes", "tasks", "topics", "all"]


# ---------------------------------------------------------------- request models
class JobOptions(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    run_async: bool = Field(False, alias="async")


class ExtractIn(JobOptions):
    source: str = SOURCE


class TranscribeIn(JobOptions):
    source: str = SOURCE
    language: str = LANGUAGE
    diarize: bool = False
    force: bool = False


class SummarizeIn(JobOptions):
    text: str | None = Field(None, max_length=2_000_000)
    transcript_uri: str | None = Field(None, max_length=1024, pattern=r"^storage://")
    kind: Kind = "all"
    model: str | None = MODEL

    @model_validator(mode="after")
    def one_input(self):
        if (self.text is None) == (self.transcript_uri is None):
            raise ValueError("envie exatamente um de 'text' ou 'transcript_uri'")
        return self


class ProcessVideoIn(JobOptions):
    source: str = SOURCE
    language: str = LANGUAGE
    kind: Kind = "all"
    model: str | None = MODEL
    diarize: bool = False
    force: bool = False
    title: str | None = Field(None, max_length=300)


class PipelineIn(JobOptions):
    source: str = SOURCE
    tenant: str = Field(min_length=1, max_length=64)
    domain: str | None = Field(None, max_length=64)
    ingest: bool = True
    language: str = LANGUAGE
    model: str | None = MODEL
    title: str | None = Field(None, max_length=300)
    diarize: bool = False
    force: bool = False


class EmbedIn(BaseModel):
    texts: list[str] = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def bounded(self):
        if any(not t.strip() or len(t) > 32_000 for t in self.texts):
            raise ValueError("cada texto precisa ter entre 1 e 32000 caracteres")
        return self


# ---------------------------------------------------------------- auth
@dataclass(frozen=True)
class Principal:
    name: str
    tenant: str | None = None  # None: EDGE_API_KEY, every tenant and route


ADMIN = Principal("admin")


def tenant_route(method: str, path: str) -> bool:
    """The only routes a tenant key may call: what the transcript_to_notes skill needs, nothing that reads
    or writes media of another tenant (transcripts, summaries, archive, retention)."""
    if (method, path) in (("POST", "/upload"), ("POST", "/pipeline"), ("GET", "/jobs")):
        return True
    return method == "GET" and path.startswith("/jobs/") and "/" not in path[len("/jobs/") :]


class AuthAndAccessLog:
    """Bearer auth (constant-time) + one JSON access-log line per request. Pure ASGI: uploads stay streamed.

    Sets scope["state"]["principal"]; a tenant key outside `tenant_route` gets 403 before any handler runs.
    """

    OPEN = frozenset({"/healthz", "/readyz"})

    def __init__(self, app: ASGIApp, api_key: str, tenant_keys: tuple[tuple[str, str], ...] = ()) -> None:
        self.app = app
        self.keys = [(api_key.encode(), ADMIN)]
        self.keys += [(key.encode(), Principal(f"tenant:{tenant}", tenant)) for tenant, key in tenant_keys]

    def _principal(self, scope: Scope) -> Principal | None:
        token = b""
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                scheme, _, rest = value.strip().partition(b" ")
                token = rest.strip() if scheme.lower() == b"bearer" else b""
                break
        found = None
        for key, principal in self.keys:  # compare against every key: timing doesn't reveal which matched
            if hmac.compare_digest(token, key):  # keys are never empty (config), so b"" never matches
                found = principal
        return found

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status = 500
        principal: Principal | None = None

        async def send_wrapper(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            if scope["path"] in self.OPEN:
                await self.app(scope, receive, send_wrapper)
                return
            principal = self._principal(scope)
            if principal is None:
                response = JSONResponse(
                    {"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"}
                )
            elif principal.tenant and not tenant_route(scope["method"], scope["path"]):
                response = JSONResponse(
                    {"error": f"a chave do tenant '{principal.tenant}' só pode usar /upload, /pipeline e /jobs"},
                    status_code=403,
                )
            else:
                scope.setdefault("state", {})["principal"] = principal
                await self.app(scope, receive, send_wrapper)
                return
            await response(scope, receive, send_wrapper)
        finally:
            level = logging.DEBUG if scope["path"] in self.OPEN and status < 400 else logging.INFO
            log.log(
                level,
                "request",
                extra={
                    "method": scope["method"],
                    "path": scope["path"],
                    "status": status,
                    "principal": principal.name if principal else None,
                    "ms": round((time.perf_counter() - started) * 1000, 1),
                },
            )


def principal_of(request: Request) -> Principal:
    return request.state.principal  # set by AuthAndAccessLog for every authenticated route


# ---------------------------------------------------------------- retention schedule
async def maintenance_loop(storage: LocalStorage, settings: Settings) -> None:
    """Retention at startup and then every EDGE_MAINTAIN_INTERVAL_HOURS; a failed sweep never stops the loop."""
    interval = settings.maintain_interval_hours * 3600
    while True:
        try:
            await asyncio.to_thread(
                retention.maintain,
                storage,
                retention_days=settings.retention_days,
                stale_hours=settings.processing_stale_hours,
            )  # logs its JSON report ("retention done")
        except Exception:
            log.exception("scheduled retention failed")
        await asyncio.sleep(interval)


# ---------------------------------------------------------------- app
def create_app(settings: Settings | None = None, *, edge: Edge | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    setup_logging(settings.log_level)
    if not settings.api_key:
        raise ConfigError("EDGE_API_KEY is required")

    state: dict[str, Any] = {}

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        state["edge"] = edge or Edge.from_settings(settings)
        state["jobs"] = JobManager(settings.job_workers, settings.jobs_max, settings.queue_max)
        settings.storage_root.mkdir(parents=True, exist_ok=True)
        maintenance = (
            asyncio.create_task(maintenance_loop(state["edge"].storage, settings), name="edge-retention")
            if settings.maintain_interval_hours > 0
            else None
        )
        log.info(
            "edge ready",
            extra={
                "storage_root": str(settings.storage_root),
                "whisper": settings.whisper_url,
                "model": settings.summary_model,
                "fallback": settings.fallback_model,
                "workers": settings.job_workers,
                "tenant_keys": sorted({t for t, _ in settings.tenant_keys}),
                "maintain_interval_hours": settings.maintain_interval_hours,
            },
        )
        yield
        if maintenance is not None:
            maintenance.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await maintenance
        state["jobs"].shutdown()
        if edge is None:
            state["edge"].close()

    app = FastAPI(title="AIOS agent-edge", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(AuthAndAccessLog, api_key=settings.api_key, tenant_keys=settings.tenant_keys)

    def ops() -> Edge:
        return state["edge"]

    async def dispatch(
        request: Request, kind: str, run_async: bool, fn: Callable[[], Any], *, tenant: str | None = None
    ):
        jobs: JobManager = state["jobs"]
        owner = principal_of(request).name
        if run_async:
            job = jobs.submit(kind, fn, owner=owner, tenant=tenant)
            return JSONResponse(
                {"job_id": job.id, "status": job.status, "status_url": f"/jobs/{job.id}"}, status_code=202
            )
        return await jobs.run(kind, fn, owner=owner, tenant=tenant)

    @app.exception_handler(EdgeError)
    async def edge_error(_: Request, exc: EdgeError):
        return JSONResponse({"error": exc.message}, status_code=exc.status)

    @app.exception_handler(StorageError)
    async def storage_error(_: Request, exc: StorageError):
        return JSONResponse({"error": f"URI inválida: {exc}"}, status_code=400)

    @app.exception_handler(Exception)
    async def unexpected(_: Request, exc: Exception):
        log.error("unhandled error", exc_info=exc)
        return JSONResponse({"error": f"erro interno ({type(exc).__name__})"}, status_code=500)

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz():
        root = settings.storage_root
        required = {
            "storage": root.is_dir() and os.access(root, os.W_OK),
            "ffmpeg": bool(shutil.which("ffmpeg") and shutil.which("ffprobe")),
        }
        async with httpx.AsyncClient(timeout=2.0) as client:
            whisper, litellm, knowledge = await asyncio.gather(
                _ping(client, f"{settings.whisper_url}/health"),
                _ping(client, f"{settings.litellm_base_url}/health/liveliness"),
                _ping(client, f"{settings.knowledge_url}/healthz"),
            )
        required["whisper"] = whisper
        ready = all(required.values())
        body = {
            "status": "ready" if ready else "not ready",
            "checks": {**required, "litellm": litellm, "knowledge": knowledge},
        }
        return JSONResponse(body, status_code=200 if ready else 503)

    @app.post("/upload", status_code=201)
    async def upload(request: Request):
        return await receive_upload(request, ops().storage, settings.max_upload_bytes)

    @app.post("/extract_audio")
    async def extract_audio(p: ExtractIn, request: Request):
        ops().source_path(p.source)
        return await dispatch(request, "extract_audio", p.run_async, lambda: ops().extract_audio(p.source))

    @app.post("/transcribe")
    async def transcribe(p: TranscribeIn, request: Request):
        if p.diarize:
            ops().diarizer()
        ops().source_path(p.source)
        return await dispatch(
            request, "transcribe", p.run_async, lambda: ops().transcribe(p.source, p.language, p.diarize, p.force)
        )

    @app.post("/summarize")
    async def summarize(p: SummarizeIn, request: Request):
        if p.transcript_uri:
            ops().source_path(p.transcript_uri)
        return await dispatch(
            request,
            "summarize",
            p.run_async,
            lambda: ops().summarize(text=p.text, transcript_uri=p.transcript_uri, kind=p.kind, model=p.model),
        )

    @app.post("/process_video")
    async def process_video(p: ProcessVideoIn, request: Request):
        if p.diarize:
            ops().diarizer()
        ops().source_path(p.source)
        return await dispatch(
            request,
            "process_video",
            p.run_async,
            lambda: ops().process_video(
                source=p.source,
                language=p.language,
                kind=p.kind,
                model=p.model,
                diarize=p.diarize,
                force=p.force,
                title=p.title,
            ),
        )

    @app.post("/pipeline")
    async def pipeline(p: PipelineIn, request: Request):
        who = principal_of(request)
        ops().check_tenant(p.tenant, p.domain)
        if who.tenant and p.tenant != who.tenant:
            raise Forbidden(f"esta chave só acessa o tenant '{who.tenant}'")
        if p.diarize:
            ops().diarizer()
        key, _ = ops().source_path(p.source)
        if who.tenant and not key.startswith("input/"):
            raise Forbidden("com chave de tenant, a fonte precisa estar em storage://media/input/")
        return await dispatch(
            request,
            "pipeline",
            p.run_async,
            lambda: ops().pipeline(
                source=p.source,
                tenant=p.tenant,
                domain=p.domain,
                ingest=p.ingest,
                language=p.language,
                model=p.model,
                title=p.title,
                diarize=p.diarize,
                force=p.force,
            ),
            tenant=p.tenant,
        )

    @app.post("/embed")
    async def embed(p: EmbedIn):
        return await run_in_threadpool(ops().embed, p.texts)

    @app.get("/jobs")
    async def list_jobs(request: Request, limit: int = 50, tenant: str | None = None):
        """Recent jobs without results; a tenant key sees only its own jobs."""
        who = principal_of(request)
        if not 1 <= limit <= 500:
            raise BadRequest("limit deve estar entre 1 e 500")
        if who.tenant and tenant not in (None, who.tenant):
            raise Forbidden(f"esta chave só acessa o tenant '{who.tenant}'")
        jobs = state["jobs"].recent(limit, owner=who.name if who.tenant else None, tenant=tenant)
        return {"jobs": [_summary(j.to_dict()) for j in jobs]}

    @app.get("/jobs/{job_id}")
    async def get_job(job_id: str, request: Request):
        who = principal_of(request)
        job = state["jobs"].get(job_id)
        if job is None or (who.tenant and job.owner != who.name):  # 404 either way: don't reveal others' ids
            raise NotFound(f"job {job_id} não encontrado (jobs vivem só na memória deste processo)")
        return job.to_dict()

    @app.post("/maintain")
    async def maintain(dry_run: bool = False):
        return await run_in_threadpool(
            lambda: retention.maintain(
                ops().storage,
                retention_days=settings.retention_days,
                stale_hours=settings.processing_stale_hours,
                dry_run=dry_run,
            )
        )

    return app


def _summary(job: dict) -> dict:
    return {k: v for k, v in job.items() if k != "result"}


async def _ping(client: httpx.AsyncClient, url: str) -> bool:
    try:
        return (await client.get(url)).status_code == 200
    except httpx.HTTPError:
        return False
