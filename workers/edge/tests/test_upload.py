import hashlib

import pytest

from .conftest import make_settings, open_client


def files_in(root, sub="media/input"):
    base = root / sub
    return sorted(p.name for p in base.iterdir()) if base.exists() else []


def test_upload_streams_into_media_input(client, settings):
    data = b"\x00\x01video-bytes" * 5000
    resp = client.post("/upload", files={"file": ("Reunião semanal.MP4", data, "video/mp4")}, data={"note": "ignored"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["uri"] == f"storage://media/input/{body['id']}.mp4"
    assert body["kind"] == "video" and body["ext"] == "mp4"
    assert body["filename"] == "Reunião semanal.MP4"
    assert body["bytes"] == len(data)
    assert body["sha256"] == hashlib.sha256(data).hexdigest()
    assert (settings.storage_root / "media" / "input" / f"{body['id']}.mp4").read_bytes() == data
    assert files_in(settings.storage_root) == [f"{body['id']}.mp4"]


def test_filename_path_components_are_dropped(client):
    resp = client.post("/upload", files={"file": ("../../etc/evil.m4a", b"abc", "audio/mp4")})
    assert resp.status_code == 201
    assert resp.json()["filename"] == "evil.m4a"
    assert resp.json()["uri"].startswith("storage://media/input/")


@pytest.mark.parametrize("name", ["script.sh", "notes.txt", "noext", "archive.mp4.exe"])
def test_extension_allowlist(client, settings, name):
    resp = client.post("/upload", files={"file": (name, b"abc", "application/octet-stream")})
    assert resp.status_code == 415
    assert files_in(settings.storage_root) == []


def test_size_limit_aborts_and_cleans_up(tmp_path):
    settings = make_settings(tmp_path, EDGE_MAX_UPLOAD_MB="1")
    with open_client(settings) as client:
        resp = client.post("/upload", files={"file": ("big.wav", b"x" * (1024 * 1024 + 1), "audio/wav")})
        assert resp.status_code == 413
        assert "EDGE_MAX_UPLOAD_MB" in resp.json()["error"]
        assert files_in(settings.storage_root) == []
        ok = client.post("/upload", files={"file": ("ok.wav", b"x" * 1000, "audio/wav")})
        assert ok.status_code == 201


def test_size_limit_is_enforced_while_streaming(tmp_path):
    """No Content-Length (chunked body): the limit must still stop the write."""
    settings = make_settings(tmp_path, EDGE_MAX_UPLOAD_MB="1")
    boundary = "edgeboundary"
    head = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="a.mp3"\r\n'
        "Content-Type: audio/mpeg\r\n\r\n"
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()

    def body():
        yield head
        for _ in range(20):
            yield b"y" * (128 * 1024)
        yield tail

    with open_client(settings) as client:
        resp = client.post(
            "/upload", content=body(), headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
        )
        assert resp.status_code == 413
        assert files_in(settings.storage_root) == []


def test_requires_multipart_and_file_field(client):
    assert (
        client.post("/upload", content=b"raw", headers={"Content-Type": "application/octet-stream"}).status_code == 415
    )
    resp = client.post("/upload", data={"other": "x"}, files={"attachment": ("a.mp3", b"abc", "audio/mpeg")})
    assert resp.status_code == 400


def test_rejects_empty_and_multiple_files(client, settings):
    assert client.post("/upload", files={"file": ("a.mp3", b"", "audio/mpeg")}).status_code == 400
    resp = client.post(
        "/upload", files=[("file", ("a.mp3", b"abc", "audio/mpeg")), ("file", ("b.mp3", b"def", "audio/mpeg"))]
    )
    assert resp.status_code == 400
    assert files_in(settings.storage_root) == []


def test_disk_full_is_507_and_cleans_up(client, settings, monkeypatch):
    import errno

    from edge import upload

    real_write = upload._Sink.on_part_data

    def full(self, data, start, end):
        if self.in_file:
            raise OSError(errno.ENOSPC, "No space left on device")
        real_write(self, data, start, end)

    monkeypatch.setattr(upload._Sink, "on_part_data", full)
    resp = client.post("/upload", files={"file": ("a.mp3", b"z" * 5000, "audio/mpeg")})
    assert resp.status_code == 507
    assert files_in(settings.storage_root) == []


@pytest.mark.parametrize(
    "head",
    [
        b'Content-Disposition: form-data; name="file"; filename="' + b"a" * 200_000 + b'.mp3"\r\n',
        b"X-Pad: " + b"p" * 200_000 + b"\r\n",
        b"".join(b"X-H%d: v\r\n" % i for i in range(50)),
    ],
    ids=["huge-filename", "huge-header", "many-headers"],
)
def test_oversized_part_headers_are_rejected_before_buffering(client, settings, head):
    boundary = "edgeboundary"
    body = (
        f"--{boundary}\r\n".encode()
        + head
        + b'Content-Disposition: form-data; name="file"; filename="a.mp3"\r\n\r\nabc'
        + f"\r\n--{boundary}--\r\n".encode()
    )
    resp = client.post("/upload", content=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    assert resp.status_code == 400 and "multipart inválido" in resp.json()["error"]
    assert files_in(settings.storage_root) == []
