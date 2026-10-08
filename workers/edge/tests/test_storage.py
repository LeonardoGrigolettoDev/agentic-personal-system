import pytest

from edge.storage import LocalStorage, StorageError, from_env, make_uri, parse_uri


@pytest.mark.parametrize(
    "uri",
    [
        "storage://media/../etc/passwd",
        "storage://media/input/../../secret",
        "storage://media//etc/passwd",
        "storage://media/a\\..\\b",
        "storage://media/a\x00b",
        "storage://Media/a",
        "storage://../a",
        "storage://media/",
        "file:///etc/passwd",
        "/etc/passwd",
    ],
)
def test_rejects_traversal_and_odd_uris(uri):
    with pytest.raises(StorageError):
        parse_uri(uri)


def test_dot_segments_normalize_inside_the_bucket():
    assert parse_uri("storage://media/./a") == ("media", "a")


def test_symlink_escape_is_rejected(tmp_path):
    root = tmp_path / "root"
    (root / "media").mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (root / "media" / "link.txt").symlink_to(outside)
    storage = LocalStorage(root)
    with pytest.raises(StorageError):
        storage.path("storage://media/link.txt")


def test_roundtrip_and_atomic_writer(tmp_path):
    storage = LocalStorage(tmp_path)
    uri = make_uri("media", "transcripts/x.txt")
    storage.put_text(uri, "olá")
    assert storage.get_text(uri) == "olá"
    assert list(storage.list("storage://media/transcripts/")) == [uri]

    with pytest.raises(RuntimeError), storage.writer(make_uri("media", "transcripts/y.txt")) as fh:
        fh.write(b"partial")
        raise RuntimeError("boom")
    assert not any(p.name.startswith("y.txt") for p in (tmp_path / "media" / "transcripts").iterdir())


def test_move_refreshes_mtime(tmp_path):
    import os

    storage = LocalStorage(tmp_path)
    src = make_uri("media", "input/a.mp4")
    storage.put(src, b"x")
    os.utime(storage.path(src), (0, 0))
    dst = storage.move(src, make_uri("media", "archive/a.mp4"))
    assert not storage.exists(src) and storage.exists(dst)
    assert storage.path(dst).stat().st_mtime > 1_000_000


def test_only_local_backend():
    with pytest.raises(StorageError):
        from_env({"STORAGE_BACKEND": "s3"})


def test_move_never_replaces_an_existing_file(tmp_path):
    storage = LocalStorage(tmp_path)
    archive = make_uri("media", "archive/sub/reuniao.mp4")
    moved = []
    for content in (b"um", b"dois", b"tres"):
        src = make_uri("media", "input/sub/reuniao.mp4")
        storage.put(src, content)
        moved.append(storage.move(src, archive))
    assert moved == [
        archive,
        make_uri("media", "archive/sub/reuniao-1.mp4"),
        make_uri("media", "archive/sub/reuniao-2.mp4"),
    ]
    assert [storage.get(u) for u in moved] == [b"um", b"dois", b"tres"]
    assert storage.available(archive) == make_uri("media", "archive/sub/reuniao-3.mp4")


def test_move_without_hard_links_still_refuses_to_overwrite(tmp_path, monkeypatch):
    import errno

    def no_links(src, dst):
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr("edge.storage.os.link", no_links)
    storage = LocalStorage(tmp_path)
    dst = make_uri("media", "archive/a.mp4")
    storage.put(dst, b"old")
    src = make_uri("media", "input/a.mp4")
    storage.put(src, b"new")
    assert storage.move(src, dst) == make_uri("media", "archive/a-1.mp4")
    assert storage.get(dst) == b"old" and not storage.exists(src)


def test_concurrent_writers_of_one_uri_do_not_share_a_temp_file(tmp_path):
    storage = LocalStorage(tmp_path)
    uri = make_uri("media", "transcripts/x.txt")
    with storage.writer(uri) as a, storage.writer(uri) as b:
        a.write(b"A")
        b.write(b"B")
        assert a.name != b.name
    assert storage.get(uri) in (b"A", b"B")
    assert [p.name for p in (tmp_path / "media/transcripts").iterdir()] == ["x.txt"]
