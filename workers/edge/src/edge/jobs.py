"""In-process job queue: one worker by default (RAM), bounded store, sync callers wait on the same queue.

Heavy operations (ffmpeg, whisper, local LLM) all go through here so a synchronous request and an
async job never run concurrently beyond EDGE_JOB_WORKERS. State lives in memory: a restart loses
queued/running jobs (their outputs already written to storage stay).
"""

import asyncio
import logging
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from edge.errors import Busy, EdgeError

log = logging.getLogger(__name__)

ACTIVE = ("queued", "running")


@dataclass
class Job:
    id: str
    kind: str
    mode: str  # async | sync
    owner: str = "admin"  # principal that submitted it: only that principal (or admin) can see it
    tenant: str | None = None
    status: str = "queued"
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    result: Any = None
    error: dict | None = None
    exc: BaseException | None = field(default=None, repr=False)
    future: Future | None = field(default=None, repr=False)

    def to_dict(self) -> dict:
        out = {
            "job_id": self.id,
            "kind": self.kind,
            "mode": self.mode,
            "owner": self.owner,
            "tenant": self.tenant,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }
        if self.status == "succeeded":
            out["result"] = self.result
        if self.error:
            out["error"] = self.error
        return out


class JobManager:
    def __init__(self, workers: int = 1, max_jobs: int = 100, max_queued: int = 20) -> None:
        self.max_jobs = max_jobs
        self.max_queued = max_queued
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="edge-job")
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._lock = threading.Lock()

    def submit(
        self, kind: str, fn: Callable[[], Any], *, mode: str = "async", owner: str = "admin", tenant: str | None = None
    ) -> Job:
        with self._lock:
            active = sum(1 for j in self._jobs.values() if j.status in ACTIVE)
            if active >= self.max_queued:
                raise Busy(f"fila cheia ({active} jobs pendentes); tente novamente mais tarde")
            self._evict()
            if len(self._jobs) >= self.max_jobs:
                raise Busy("limite de jobs em memória atingido; tente novamente mais tarde")
            job = Job(id=uuid.uuid4().hex, kind=kind, mode=mode, owner=owner, tenant=tenant)
            self._jobs[job.id] = job
        log.info("job queued", extra={"job_id": job.id, "kind": kind, "mode": mode})
        job.future = self._executor.submit(self._run, job, fn)
        job.future.add_done_callback(lambda f, j=job: self._on_done(j, f))
        return job

    async def run(self, kind: str, fn: Callable[[], Any], *, owner: str = "admin", tenant: str | None = None) -> Any:
        """Queue and wait (synchronous HTTP callers). Re-raises the operation's own exception."""
        job = self.submit(kind, fn, mode="sync", owner=owner, tenant=tenant)
        await asyncio.wrap_future(job.future)
        if job.exc is not None:
            raise job.exc
        result, job.result = job.result, None  # the caller has it; don't keep large payloads in memory
        return result

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def recent(self, limit: int = 50, *, owner: str | None = None, tenant: str | None = None) -> list[Job]:
        with self._lock:
            jobs = [
                j
                for j in reversed(self._jobs.values())
                if (owner is None or j.owner == owner) and (tenant is None or j.tenant == tenant)
            ]
        return jobs[:limit]

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _run(self, job: Job, fn: Callable[[], Any]) -> None:
        job.status, job.started_at = "running", time.time()
        log.info("job started", extra={"job_id": job.id, "kind": job.kind})
        try:
            job.result = fn()
            job.status = "succeeded"
        except EdgeError as exc:
            job.exc, job.error, job.status = exc, exc.to_dict(), "failed"
        except Exception as exc:
            log.exception("job crashed", extra={"job_id": job.id, "kind": job.kind})
            job.exc, job.error, job.status = (
                exc,
                {"status": 500, "error": f"erro interno ({type(exc).__name__})"},
                "failed",
            )
        finally:
            job.finished_at = time.time()
            log.info(
                "job finished",
                extra={
                    "job_id": job.id,
                    "kind": job.kind,
                    "status": job.status,
                    "seconds": round(job.finished_at - job.started_at, 3),
                },
            )

    def _on_done(self, job: Job, future: Future) -> None:
        if future.cancelled():  # sync caller went away (or shutdown) before the job started
            job.status, job.finished_at = "cancelled", time.time()

    def _evict(self) -> None:
        """Drop the oldest finished jobs until there is room for one more."""
        while len(self._jobs) >= self.max_jobs:
            victim = next((jid for jid, j in self._jobs.items() if j.status not in ACTIVE), None)
            if victim is None:
                return
            del self._jobs[victim]
