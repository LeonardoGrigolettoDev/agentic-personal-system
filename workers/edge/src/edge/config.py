"""Settings from environment variables only (same names in Compose and on Railway)."""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

MB = 1024 * 1024
MIN_TENANT_KEY_CHARS = 16


class ConfigError(ValueError):
    pass


def _str(env: Mapping[str, str], name: str, default: str) -> str:
    return (env.get(name) or "").strip() or default


def _num(env: Mapping[str, str], name: str, default: float, *, minimum: float, cast=float):
    raw = (env.get(name) or "").strip()
    if not raw:
        return cast(default)
    try:
        value = cast(raw)
    except ValueError:
        raise ConfigError(f"{name}={raw!r} is not a number") from None
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def _base_url(raw: str) -> str:
    """LiteLLM-style base: no trailing slash and no trailing /v1 (clients append /v1/...)."""
    url = raw.rstrip("/")
    return url[: -len("/v1")] if url.endswith("/v1") else url


def _optional_model(name: str) -> str | None:
    return None if name.lower() in ("none", "off", "-") else name


def _tenant_keys(raw: str, tenants: tuple[str, ...], admin_key: str) -> tuple[tuple[str, str], ...]:
    """EDGE_TENANT_KEYS='nitro:<key>,pessoal:<key>' -> ((tenant, key), ...). Errors never echo a key."""
    pairs: list[tuple[str, str]] = []
    for item in (p.strip() for p in raw.split(",")):
        if not item:
            continue
        tenant, sep, key = (part.strip() for part in item.partition(":"))
        if not sep or not tenant or not key:
            raise ConfigError("EDGE_TENANT_KEYS must look like 'tenant:key,tenant:key'")
        if tenant not in tenants:
            raise ConfigError(f"EDGE_TENANT_KEYS: unknown tenant {tenant!r} (KNOWLEDGE_TENANTS={','.join(tenants)})")
        if len(key) < MIN_TENANT_KEY_CHARS:
            raise ConfigError(f"EDGE_TENANT_KEYS: the key for {tenant!r} is shorter than {MIN_TENANT_KEY_CHARS} chars")
        if key == admin_key or any(key == k for _, k in pairs):
            raise ConfigError("EDGE_TENANT_KEYS: every key must be unique and differ from EDGE_API_KEY")
        pairs.append((tenant, key))
    return tuple(pairs)


@dataclass(frozen=True)
class Settings:
    api_key: str = field(repr=False)
    storage_root: Path
    max_upload_bytes: int
    whisper_url: str
    whisper_timeout: float
    whisper_chunk_seconds: int
    ffmpeg_timeout: float
    litellm_base_url: str
    litellm_key: str | None = field(repr=False)
    summary_model: str
    fallback_model: str | None
    embed_model: str
    map_chunk_tokens: int
    fallback_chunk_tokens: int
    local_max_tokens: int
    llm_timeout: float
    knowledge_url: str
    knowledge_key: str | None = field(repr=False)
    knowledge_timeout: float
    tenants: tuple[str, ...]
    retention_days: int
    processing_stale_hours: float
    maintain_interval_hours: float
    job_workers: int
    jobs_max: int
    queue_max: int
    hf_token: str | None = field(repr=False)
    diarize_model: str
    log_level: str
    # (tenant, key) pairs: a request with one of these keys may only act on that tenant (CONTRACTS §4 isolation)
    tenant_keys: tuple[tuple[str, str], ...] = field(default=(), repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        backend = _str(env, "STORAGE_BACKEND", "local")
        if backend != "local":
            raise ConfigError(
                f"edge needs local files for ffmpeg/whisper; STORAGE_BACKEND={backend!r} is not supported"
            )
        api_key = (env.get("EDGE_API_KEY") or "").strip()
        tenants = tuple(
            t.strip() for t in _str(env, "KNOWLEDGE_TENANTS", "nitro,pessoal,shared").split(",") if t.strip()
        )
        return cls(
            api_key=api_key,
            storage_root=Path(_str(env, "STORAGE_LOCAL_ROOT", "/data/storage")).expanduser(),
            max_upload_bytes=_num(env, "EDGE_MAX_UPLOAD_MB", 2048, minimum=1, cast=int) * MB,
            whisper_url=_str(env, "WHISPER_URL", "http://whisper:8080").rstrip("/"),
            whisper_timeout=_num(env, "EDGE_WHISPER_TIMEOUT", 1800, minimum=1),
            whisper_chunk_seconds=_num(env, "EDGE_WHISPER_CHUNK_SECONDS", 600, minimum=0, cast=int),
            ffmpeg_timeout=_num(env, "EDGE_FFMPEG_TIMEOUT", 7200, minimum=1),
            litellm_base_url=_base_url(_str(env, "LITELLM_BASE_URL", "http://litellm:4000")),
            litellm_key=(env.get("EDGE_LITELLM_KEY") or "").strip() or None,
            summary_model=_str(env, "EDGE_SUMMARY_MODEL", "local-qwen"),
            fallback_model=_optional_model(_str(env, "EDGE_FALLBACK_MODEL", "tier2-cheap")),
            embed_model=_str(env, "EDGE_EMBED_MODEL", "embed-local"),
            map_chunk_tokens=_num(env, "EDGE_MAP_CHUNK_TOKENS", 2500, minimum=200, cast=int),
            fallback_chunk_tokens=_num(env, "EDGE_FALLBACK_CHUNK_TOKENS", 24000, minimum=200, cast=int),
            local_max_tokens=_num(env, "EDGE_LOCAL_MAX_TOKENS", 24000, minimum=200, cast=int),
            llm_timeout=_num(env, "EDGE_LLM_TIMEOUT", 300, minimum=1),
            knowledge_url=_str(env, "KNOWLEDGE_URL", "http://knowledge:8080").rstrip("/"),
            knowledge_key=(env.get("KNOWLEDGE_API_KEY") or "").strip() or None,
            knowledge_timeout=_num(env, "EDGE_KNOWLEDGE_TIMEOUT", 300, minimum=1),
            tenants=tenants,
            retention_days=_num(env, "MEDIA_RETENTION_DAYS", 30, minimum=0, cast=int),
            processing_stale_hours=_num(env, "EDGE_PROCESSING_STALE_HOURS", 24, minimum=0),
            maintain_interval_hours=_num(env, "EDGE_MAINTAIN_INTERVAL_HOURS", 24, minimum=0),
            job_workers=_num(env, "EDGE_JOB_WORKERS", 1, minimum=1, cast=int),
            jobs_max=_num(env, "EDGE_JOBS_MAX", 100, minimum=1, cast=int),
            queue_max=_num(env, "EDGE_QUEUE_MAX", 20, minimum=1, cast=int),
            hf_token=(env.get("HF_TOKEN") or "").strip() or None,
            diarize_model=_str(env, "EDGE_DIARIZE_MODEL", "pyannote/speaker-diarization-community-1"),
            log_level=_str(env, "EDGE_LOG_LEVEL", "INFO").upper(),
            tenant_keys=_tenant_keys(env.get("EDGE_TENANT_KEYS") or "", tenants, api_key),
        )
