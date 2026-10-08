import json
import os
import time

from edge import cli, retention
from edge.storage import LocalStorage

DAY = 86400


def touch(path, age_seconds, now, data=b"x" * 10):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.utime(path, (now - age_seconds, now - age_seconds))
    return path


def tree(tmp_path, now):
    media = tmp_path / "media"
    return {
        "old_archive": touch(media / "archive/old.mp4", 31 * DAY, now),
        "new_archive": touch(media / "archive/new.mp4", 29 * DAY, now),
        "stale_wav": touch(media / "processing/a.wav", 25 * 3600, now),
        "stale_chunk": touch(media / "processing/a.chunks/chunk-0000.wav", 25 * 3600, now),
        "fresh_wav": touch(media / "processing/b.wav", 3600, now),
        "stale_upload_part": touch(media / "input/c.mp4.part", 25 * 3600, now),
        "waiting_upload": touch(media / "input/d.mp4", 90 * DAY, now),
        "transcript": touch(media / "transcripts/e.txt", 400 * DAY, now),
    }


def test_maintain_applies_retention(tmp_path):
    now = time.time()
    files = tree(tmp_path, now)
    report = retention.maintain(LocalStorage(tmp_path), retention_days=30, stale_hours=24, now=now)
    assert report["archive_deleted"] == 1 and report["processing_deleted"] == 2 and report["partials_deleted"] == 1
    assert report["bytes_freed"] == 40 and report["errors"] == 0
    gone = {"old_archive", "stale_wav", "stale_chunk", "stale_upload_part"}
    for name, path in files.items():
        assert path.exists() == (name not in gone), name
    assert not (tmp_path / "media/processing/a.chunks").exists()
    assert (tmp_path / "media/processing").is_dir()


def test_dry_run_deletes_nothing(tmp_path):
    now = time.time()
    files = tree(tmp_path, now)
    report = retention.maintain(LocalStorage(tmp_path), retention_days=30, stale_hours=24, now=now, dry_run=True)
    assert report["archive_deleted"] == 1 and report["dry_run"] is True
    assert all(p.exists() for p in files.values())


def test_zero_days_keeps_archive_forever(tmp_path):
    now = time.time()
    files = tree(tmp_path, now)
    retention.maintain(LocalStorage(tmp_path), retention_days=0, stale_hours=24, now=now)
    assert files["old_archive"].exists()


def test_symlinks_are_not_followed(tmp_path):
    now = time.time()
    outside = tmp_path / "outside"
    victim = touch(outside / "keep.mp4", 400 * DAY, now)
    archive = tmp_path / "root/media/archive"
    archive.mkdir(parents=True)
    (archive / "linkdir").symlink_to(outside, target_is_directory=True)
    retention.maintain(LocalStorage(tmp_path / "root"), retention_days=30, stale_hours=24, now=now)
    assert victim.exists()


def test_cli_maintain_prints_report(tmp_path, monkeypatch, capsys):
    now = time.time()
    tree(tmp_path, now)
    monkeypatch.setenv("STORAGE_LOCAL_ROOT", str(tmp_path))
    monkeypatch.setenv("MEDIA_RETENTION_DAYS", "30")
    monkeypatch.delenv("EDGE_API_KEY", raising=False)
    assert cli.main(["maintain"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["archive_deleted"] == 1 and report["processing_deleted"] == 2


def test_maintain_endpoint(client, settings):
    now = time.time()
    touch(settings.storage_root / "media/archive/old.mp4", 31 * DAY, now)
    assert client.post("/maintain", params={"dry_run": True}).json()["archive_deleted"] == 1
    assert (settings.storage_root / "media/archive/old.mp4").exists()
    assert client.post("/maintain").json()["archive_deleted"] == 1
    assert not (settings.storage_root / "media/archive/old.mp4").exists()


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_retention_runs_at_startup_and_on_schedule(tmp_path, monkeypatch):
    from .conftest import make_settings, open_client

    runs = []
    real = retention.maintain
    monkeypatch.setattr(retention, "maintain", lambda *a, **kw: runs.append(kw) or real(*a, **kw))
    settings = make_settings(tmp_path, EDGE_MAINTAIN_INTERVAL_HOURS="0.0001")  # every 0.36 s
    now = time.time()
    old = touch(settings.storage_root / "media/archive/old.mp4", 31 * DAY, now)
    stale = touch(settings.storage_root / "media/processing/x.1234abcd.wav", 25 * 3600, now)
    with open_client(settings) as client:
        assert _wait_until(lambda: not old.exists() and not stale.exists()), "startup sweep did not run"
        assert _wait_until(lambda: len(runs) >= 2), "no periodic sweep"
        assert client.get("/healthz").status_code == 200
    count = len(runs)
    time.sleep(0.5)
    assert len(runs) == count  # the loop stopped with the app
    assert runs[0] == {"retention_days": 30, "stale_hours": 24.0}


def test_schedule_survives_a_failing_sweep(tmp_path, monkeypatch):
    from .conftest import make_settings, open_client

    calls = []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("disk hiccup")
        return {}

    monkeypatch.setattr(retention, "maintain", flaky)
    with open_client(make_settings(tmp_path, EDGE_MAINTAIN_INTERVAL_HOURS="0.0001")):
        assert _wait_until(lambda: len(calls) >= 2)


def test_zero_interval_disables_the_schedule(tmp_path):
    from .conftest import make_settings, open_client

    settings = make_settings(tmp_path, EDGE_MAINTAIN_INTERVAL_HOURS="0")
    old = touch(settings.storage_root / "media/archive/old.mp4", 31 * DAY, time.time())
    with open_client(settings) as client:
        client.get("/jobs")
        time.sleep(0.2)
    assert old.exists()
