"""Abstract file storage (docs/ARCHITECTURE.md §24.11): `storage://<bucket>/<key>` references.

The application never stores host paths. The backend is chosen by env:
  STORAGE_BACKEND=local  -> files under STORAGE_LOCAL_ROOT (V1: ./data/storage)
  STORAGE_BACKEND=s3     -> any S3-compatible bucket (Cloudflare R2, Railway Bucket) via STORAGE_S3_*
so moving to the cloud is a copy (infra/scripts/storage-sync.sh) plus an env change.
"""

import os
import re
from collections.abc import Iterator, Mapping
from pathlib import Path, PurePosixPath
from typing import Protocol

SCHEME = "storage://"
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


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


class Storage(Protocol):
    def put(self, uri: str, data: bytes) -> str: ...
    def get(self, uri: str) -> bytes: ...
    def exists(self, uri: str) -> bool: ...
    def delete(self, uri: str) -> None: ...
    def list(self, prefix_uri: str) -> Iterator[str]: ...


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
        p = self.path(uri)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".part")
        tmp.write_bytes(data)
        tmp.replace(p)
        return uri

    def get(self, uri: str) -> bytes:
        return self.path(uri).read_bytes()

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


class S3Storage:
    """S3-compatible backend. Buckets map to key prefixes inside one physical bucket."""

    def __init__(self, bucket: str, *, endpoint_url: str | None, access_key: str, secret_key: str, region: str = "auto") -> None:
        import boto3  # optional extra: uv sync --extra s3

        self.bucket = bucket
        self.client = boto3.client(
            "s3", endpoint_url=endpoint_url, aws_access_key_id=access_key,
            aws_secret_access_key=secret_key, region_name=region,
        )

    def _key(self, uri: str) -> str:
        bucket, key = parse_uri(uri)
        return f"{bucket}/{key}"

    def put(self, uri: str, data: bytes) -> str:
        self.client.put_object(Bucket=self.bucket, Key=self._key(uri), Body=data)
        return uri

    def get(self, uri: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=self._key(uri))["Body"].read()

    def exists(self, uri: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=self._key(uri))
            return True
        except self.client.exceptions.ClientError:
            return False

    def delete(self, uri: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=self._key(uri))

    def list(self, prefix_uri: str) -> Iterator[str]:
        prefix = prefix_uri[len(SCHEME) :]
        for page in self.client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                yield SCHEME + obj["Key"]


def from_env(env: Mapping[str, str] | None = None) -> Storage:
    env = os.environ if env is None else env
    backend = env.get("STORAGE_BACKEND", "local")
    if backend == "local":
        return LocalStorage(Path(env.get("STORAGE_LOCAL_ROOT", "data/storage")).expanduser())
    if backend == "s3":
        return S3Storage(
            env["STORAGE_S3_BUCKET"], endpoint_url=env.get("STORAGE_S3_ENDPOINT") or None,
            access_key=env["STORAGE_S3_ACCESS_KEY_ID"], secret_key=env["STORAGE_S3_SECRET_ACCESS_KEY"],
            region=env.get("STORAGE_S3_REGION", "auto"),
        )
    raise StorageError(f"unknown STORAGE_BACKEND {backend!r}")
