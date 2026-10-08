"""EDGE_TENANT_KEYS: a session's key reaches only its own tenant and its own jobs (CONTRACTS §4 isolation)."""

import json
import time

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from edge.app import create_app
from edge.config import ConfigError

from .conftest import (
    GOOD_SUMMARY,
    KEY,
    KNOWLEDGE,
    LITELLM,
    WHISPER,
    chat_response,
    make_settings,
    put_media,
    whisper_json,
)

NITRO = "nitro-key-0123456789abcdef"
PESSOAL = "pessoal-key-0123456789abcdef"
UUID = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path, EDGE_TENANT_KEYS=f"nitro:{NITRO}, pessoal:{PESSOAL}")


@pytest.fixture
def app_client(settings):
    with TestClient(create_app(settings)) as c:
        yield c


def as_(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def mock_backends():
    respx.post(f"{WHISPER}/inference").mock(return_value=httpx.Response(200, json=whisper_json()))
    respx.post(f"{LITELLM}/v1/chat/completions").mock(return_value=chat_response(json.dumps(GOOD_SUMMARY)))
    return respx.post(f"{KNOWLEDGE}/v1/ingest").mock(
        side_effect=lambda req: httpx.Response(
            200, json={"action": "new", "source_uri": json.loads(req.content)["source_uri"]}
        )
    )


def wait_done(client, key, job_id):
    for _ in range(500):
        job = client.get(f"/jobs/{job_id}", headers=as_(key)).json()
        if job["status"] not in ("queued", "running"):
            return job
        time.sleep(0.01)
    raise AssertionError("job did not finish")


@pytest.mark.parametrize(
    "raw,problem",
    [
        ("work:abcdefghijklmnop0123", "unknown tenant"),
        ("nitro:short", "shorter"),
        (f"nitro:{NITRO},pessoal:{NITRO}", "unique"),
        ("nitro:admin-key-0123456789abcdef", "unique"),
        ("nitro", "tenant:key"),
    ],
)
def test_tenant_keys_are_validated_without_echoing_them(tmp_path, raw, problem):
    with pytest.raises(ConfigError, match=problem) as err:
        make_settings(tmp_path, EDGE_API_KEY="admin-key-0123456789abcdef", EDGE_TENANT_KEYS=raw)
    assert all(secret not in str(err.value) for secret in (NITRO, "admin-key-0123456789abcdef", ":short"))


def test_settings_repr_hides_secrets(settings):
    assert NITRO not in repr(settings) and KEY not in repr(settings) and "knowledge-key" not in repr(settings)


@pytest.mark.parametrize(
    "method,path",
    [
        ("post", "/transcribe"),
        ("post", "/summarize"),
        ("post", "/extract_audio"),
        ("post", "/process_video"),
        ("post", "/embed"),
        ("post", "/maintain"),
        ("get", "/openapi.json"),
        ("get", "/jobs/x/y"),
    ],
)
def test_tenant_keys_only_reach_the_skill_routes(app_client, method, path):
    resp = app_client.request(method, path, headers=as_(NITRO), json={})
    assert resp.status_code == 403 and "nitro" in resp.json()["error"]


@respx.mock
def test_pipeline_is_bound_to_the_key_tenant_and_the_input_folder(app_client, settings, ffmpeg):
    mock_backends()
    uri = put_media(settings, f"input/{UUID}.m4a")
    other = app_client.post("/pipeline", json={"source": uri, "tenant": "pessoal"}, headers=as_(NITRO))
    assert other.status_code == 403
    shared = app_client.post("/pipeline", json={"source": uri, "tenant": "shared"}, headers=as_(NITRO))
    assert shared.status_code == 403
    archived = put_media(settings, "archive/reuniao-pessoal.m4a")
    assert (
        app_client.post("/pipeline", json={"source": archived, "tenant": "nitro"}, headers=as_(NITRO)).status_code
        == 403
    )
    assert ffmpeg.calls == []
    ok = app_client.post("/pipeline", json={"source": uri, "tenant": "nitro"}, headers=as_(NITRO))
    assert ok.status_code == 200, ok.text
    assert ok.json()["tenant"] == "nitro"


@respx.mock
def test_jobs_are_visible_only_to_the_key_that_created_them(app_client, settings, ffmpeg):
    mock_backends()
    jobs = {}
    for name, key, upload_id in (("nitro", NITRO, "a" * 32), ("pessoal", PESSOAL, "b" * 32)):
        uri = put_media(settings, f"input/{upload_id}.m4a")
        resp = app_client.post("/pipeline", json={"source": uri, "tenant": name, "async": True}, headers=as_(key))
        assert resp.status_code == 202, resp.text
        jobs[name] = resp.json()["job_id"]
        assert wait_done(app_client, key, jobs[name])["status"] == "succeeded"

    peek = app_client.get(f"/jobs/{jobs['pessoal']}", headers=as_(NITRO))
    assert peek.status_code == 404  # same answer as an unknown id
    listed = app_client.get("/jobs", headers=as_(NITRO)).json()["jobs"]
    assert [j["job_id"] for j in listed] == [jobs["nitro"]] and "result" not in listed[0]
    assert app_client.get("/jobs", params={"tenant": "pessoal"}, headers=as_(NITRO)).status_code == 403
    own = app_client.get(f"/jobs/{jobs['nitro']}", headers=as_(NITRO)).json()
    assert own["tenant"] == "nitro" and own["owner"] == "tenant:nitro" and own["result"]["txt_uri"]

    admin = app_client.get("/jobs", headers=as_(KEY)).json()["jobs"]
    assert {j["job_id"] for j in admin} == set(jobs.values())
    only_pessoal = app_client.get("/jobs", params={"tenant": "pessoal"}, headers=as_(KEY)).json()["jobs"]
    assert [j["job_id"] for j in only_pessoal] == [jobs["pessoal"]]
    assert app_client.get(f"/jobs/{jobs['nitro']}", headers=as_(KEY)).status_code == 200


def test_tenant_key_can_upload(app_client):
    resp = app_client.post("/upload", files={"file": ("aula.m4a", b"abc", "audio/mp4")}, headers=as_(PESSOAL))
    assert resp.status_code == 201 and resp.json()["uri"].startswith("storage://media/input/")


def test_admin_key_keeps_every_route(app_client):
    assert app_client.post("/maintain", params={"dry_run": True}, headers=as_(KEY)).status_code == 200
