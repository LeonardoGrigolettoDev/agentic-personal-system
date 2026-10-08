import asyncio
import threading
import time

import httpx
import pytest
import respx

from edge.errors import Busy, Unprocessable
from edge.jobs import JobManager

from .conftest import WHISPER, put_media, whisper_json


def wait_for(client, job_id, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/jobs/{job_id}").json()
        if job["status"] not in ("queued", "running"):
            return job
        time.sleep(0.02)
    raise AssertionError("job did not finish")


@respx.mock
def test_async_transcribe_job(client, settings, ffmpeg):
    respx.post(f"{WHISPER}/inference").mock(return_value=httpx.Response(200, json=whisper_json()))
    uri = put_media(settings, "input/ffffffffffffffffffffffffffffffff.mp3")
    resp = client.post("/transcribe", json={"source": uri, "async": True})
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]
    assert resp.json()["status_url"] == f"/jobs/{job_id}"
    job = wait_for(client, job_id)
    assert job["status"] == "succeeded" and job["kind"] == "transcribe"
    assert job["result"]["txt_uri"].endswith(".txt")
    listed = client.get("/jobs").json()["jobs"]
    assert listed[0]["job_id"] == job_id and "result" not in listed[0]


def test_async_job_failure_is_recorded(client, settings, ffmpeg):
    ffmpeg.fail_with = "corrupt"
    uri = put_media(settings, "input/bad.mp4")
    job = wait_for(client, client.post("/extract_audio", json={"source": uri, "async": True}).json()["job_id"])
    assert job["status"] == "failed"
    assert job["error"]["status"] == 422 and "corrupt" in job["error"]["error"]


def test_validation_errors_are_immediate_even_when_async(client):
    resp = client.post("/transcribe", json={"source": "storage://media/input/nope.mp3", "async": True})
    assert resp.status_code == 404


def test_unknown_job_is_404(client):
    assert client.get("/jobs/does-not-exist").status_code == 404


def test_single_worker_runs_jobs_one_at_a_time():
    jobs = JobManager(workers=1, max_jobs=10, max_queued=10)
    running, peak, lock = [0], [0], threading.Lock()

    def work():
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.05)
        with lock:
            running[0] -= 1
        return "ok"

    submitted = [jobs.submit("t", work) for _ in range(4)]
    for job in submitted:
        job.future.result(timeout=5)
    assert peak[0] == 1 and all(j.status == "succeeded" for j in submitted)
    jobs.shutdown()


def test_queue_limit_and_bounded_store():
    jobs = JobManager(workers=1, max_jobs=3, max_queued=2)
    gate = threading.Event()
    a = jobs.submit("t", gate.wait)
    jobs.submit("t", lambda: 1)
    with pytest.raises(Busy):
        jobs.submit("t", lambda: 1)
    gate.set()
    a.future.result(timeout=5)
    time.sleep(0.05)
    for _ in range(5):  # finished jobs are evicted oldest-first, the store never grows past max_jobs
        jobs.submit("t", lambda: 1).future.result(timeout=5)
    assert len(jobs.recent(100)) <= 3
    assert jobs.get(a.id) is None
    jobs.shutdown()


def test_sync_run_reraises_and_drops_result():
    jobs = JobManager()

    def fail():
        raise Unprocessable("ruim")

    async def scenario():
        with pytest.raises(Unprocessable):
            await jobs.run("t", fail)
        return await jobs.run("t", lambda: {"big": "x"})

    assert asyncio.run(scenario()) == {"big": "x"}
    assert all(j.result is None for j in jobs.recent())
    jobs.shutdown()
