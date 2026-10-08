import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response

from edge.app import create_app
from edge.config import ConfigError

from .conftest import KEY, KNOWLEDGE, LITELLM, WHISPER, make_settings


@pytest.fixture
def anon(settings):
    with TestClient(create_app(settings)) as c:
        yield c


def test_health_is_open(anon):
    assert anon.get("/healthz").json() == {"status": "ok"}


@pytest.mark.parametrize("header", [None, "Bearer wrong", f"Basic {KEY}", "Bearer", f"Bearer {KEY}x"])
def test_everything_else_requires_the_key(anon, header):
    headers = {"Authorization": header} if header else {}
    for method, path in [
        ("get", "/jobs"),
        ("post", "/embed"),
        ("post", "/upload"),
        ("get", "/openapi.json"),
        ("post", "/pipeline"),
        ("get", "/jobs/abc"),
    ]:
        resp = anon.request(method, path, headers=headers)
        assert resp.status_code == 401, (method, path)
        assert resp.headers["www-authenticate"] == "Bearer"


def test_correct_key_passes(anon):
    assert anon.get("/jobs", headers={"Authorization": f"Bearer {KEY}"}).status_code == 200
    assert anon.get("/jobs", headers={"Authorization": f"bearer {KEY}"}).status_code == 200


def test_api_key_is_mandatory(tmp_path):
    with pytest.raises(ConfigError):
        create_app(make_settings(tmp_path, EDGE_API_KEY=""))


@respx.mock
def test_readyz_reports_dependencies(anon, monkeypatch):
    monkeypatch.setattr("edge.app.shutil.which", lambda name: f"/usr/bin/{name}")
    respx.get(f"{WHISPER}/health").mock(return_value=Response(200, json={"status": "ok"}))
    respx.get(f"{LITELLM}/health/liveliness").mock(return_value=Response(200))
    respx.get(f"{KNOWLEDGE}/healthz").mock(return_value=Response(503))
    resp = anon.get("/readyz")
    assert resp.status_code == 200
    assert resp.json()["checks"] == {
        "storage": True,
        "ffmpeg": True,
        "whisper": True,
        "litellm": True,
        "knowledge": False,
    }


@respx.mock
def test_readyz_fails_without_whisper_or_ffmpeg(anon, monkeypatch):
    monkeypatch.setattr("edge.app.shutil.which", lambda name: None)
    respx.get(f"{WHISPER}/health").mock(return_value=Response(503, json={"status": "loading model"}))
    respx.get(f"{LITELLM}/health/liveliness").mock(return_value=Response(200))
    respx.get(f"{KNOWLEDGE}/healthz").mock(return_value=Response(200))
    resp = anon.get("/readyz")
    assert resp.status_code == 503
    assert resp.json()["checks"]["ffmpeg"] is False and resp.json()["checks"]["whisper"] is False
