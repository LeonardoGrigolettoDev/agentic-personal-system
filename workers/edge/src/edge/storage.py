"""`storage://<bucket>/<key>` resolution, copied from workers/kb/src/kb/storage.py (LocalStorage only).

URI parsing and path resolution must stay identical to kb so both services agree on what a URI means.
Edge needs real files (ffmpeg, whisper uploads), so only the local backend is supported here.
"""

import errno
import os
import re
import secrets
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import BinaryIO

SCHEME = "storage://"
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
# link(2) failures that mean "no hard links here" (FUSE, some network/overlay filesystems), not a real error.
_NO_HARDLINKS = {errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EMLINK, errno.ENOSYS}
_MAX_RENAME_SUFFIX = 1000


class StorageError(ValueError):
    pass


def parse_uri(uri: str) -> tuple[str, str]:
    """Split storage://bucket/key, rejecting traversal, absolute keys and odd buckets."""
    if not uri.startswith(SCHEME):
        raise StorageError(f"not a storage URI: {uri!r}")
    bucket, _, key = uri[len(SCHEME) :].partition("/")
    if not _BUCKET.match(bucket):
        raise StorageError(f"invalid bucket {bucket!r}")
    parts = PurePosixPath(key).parts
    if not key or key.startswith("/") or any(p in ("..", ".") for p in parts) or "\\" in key or "\x00" in key:
        raise StorageError(f"invalid key {key!r}")
    return bucket, "/".join(parts)


def make_uri(bucket: str, key: str) -> str:
    uri = f"{SCHEME}{bucket}/{key.lstrip('/')}"
    parse_uri(uri)
    return uri


class LocalStorage:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def path(self, uri: str) -> Path:
        """Host path for a URI - only for local processing (ffmpeg, whisper), never persisted."""
        bucket, key = parse_uri(uri)
        p = (self.root / bucket / key).resolve()
        if not p.is_relative_to(self.root):
            raise StorageError(f"{uri!r} escapes the storage root")
        return p

    def put(self, uri: str, data: bytes) -> str:
        with self.writer(uri) as fh:
            fh.write(data)
        return uri

    def put_text(self, uri: str, text: str) -> str:
        return self.put(uri, text.encode("utf-8"))

    def get(self, uri: str) -> bytes:
        return self.path(uri).read_bytes()

    def get_text(self, uri: str) -> str:
        return self.get(uri).decode("utf-8", errors="replace")

    def exists(self, uri: str) -> bool:
        return self.path(uri).is_file()

    def delete(self, uri: str) -> None:
        self.path(uri).unlink(missing_ok=True)

    def list(self, prefix_uri: str) -> Iterator[str]:
        bucket, _, prefix = prefix_uri[len(SCHEME) :].partition("/")
        base = self.root / bucket
        if not base.is_dir():
            return
        for p in sorted(base.rglob("*")):
            rel = p.relative_to(base).as_posix()
            if p.is_file() and rel.startswith(prefix) and not rel.endswith(".part"):
                yield f"{SCHEME}{bucket}/{rel}"

    @contextmanager
    def writer(self, uri: str) -> Iterator[BinaryIO]:
        """Atomic streaming write: data lands in `<name>.<random>.part` and replaces the target only on success.

        The random infix keeps two jobs writing the same URI from truncating each other's temp file.
        """
        p = self.path(uri)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f"{p.name}.{secrets.token_hex(4)}.part")
        try:
            with tmp.open("wb") as fh:
                yield fh
            tmp.replace(p)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def available(self, uri: str) -> str:
        """`uri` when nothing is stored there, else the first free `<stem>-<n><suffix>` next to it."""
        for candidate in _candidates(uri):
            if not os.path.lexists(self.path(candidate)):
                return candidate
        raise StorageError(f"no free name next to {uri!r}")

    def move(self, src_uri: str, dst_uri: str) -> str:
        """Rename within the storage root (same filesystem) without ever replacing an existing file.

        A taken `dst_uri` becomes the next free `<stem>-<n><suffix>`. Returns the final URI; the moved
        file's mtime becomes 'now' (retention counts from the move).
        """
        src = self.path(src_uri)
        for candidate in _candidates(dst_uri):
            dst = self.path(candidate)
            dst.parent.mkdir(parents=True, exist_ok=True)
            if _rename_noreplace(src, dst):
                os.utime(dst)
                return candidate
        raise StorageError(f"no free name next to {dst_uri!r}")


def _candidates(uri: str) -> Iterator[str]:
    bucket, key = parse_uri(uri)
    path = PurePosixPath(key)
    yield uri
    for n in range(1, _MAX_RENAME_SUFFIX):
        yield make_uri(bucket, str(path.with_name(f"{path.stem}-{n}{path.suffix}")))


def _rename_noreplace(src: Path, dst: Path) -> bool:
    """Atomic rename that refuses to replace `dst` (hard link + unlink); False when `dst` already exists."""
    try:
        os.link(src, dst)
    except FileExistsError:
        return False
    except OSError as exc:
        if exc.errno not in _NO_HARDLINKS:
            raise
        if os.path.lexists(dst):  # no hard links on this filesystem: check-then-rename, best effort
            return False
        os.replace(src, dst)
        return True
    os.unlink(src)
    return True


def from_env(env: Mapping[str, str] | None = None) -> LocalStorage:
    env = os.environ if env is None else env
    backend = env.get("STORAGE_BACKEND", "local")
    if backend != "local":
        raise StorageError(f"edge needs local files for ffmpeg/whisper; STORAGE_BACKEND={backend!r} is not supported")
    return LocalStorage(Path(env.get("STORAGE_LOCAL_ROOT", "/data/storage")).expanduser())
