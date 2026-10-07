"""Settings from the environment, with the repo-root .env as a fallback (real env vars always win)."""

import os
import re
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def parse_dotenv(text: str) -> dict[str, str]:
    """Parse KEY=VALUE lines: supports `export`, comments, and single/double quotes."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        m = _LINE.match(raw)
        if not m:
            continue
        key, value = m.groups()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            quote_char, value = value[0], value[1:-1]
            if quote_char == '"':
                value = value.replace("\\n", "\n").replace('\\"', '"')
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        values[key] = value
    return values


def load_dotenv(path: Path, environ: MutableMapping[str, str] | None = None) -> dict[str, str]:
    """Copy variables from `path` into `environ` without overriding keys already present.

    Returns the variables that were actually set.
    """
    environ = os.environ if environ is None else environ
    applied = {}
    for key, value in parse_dotenv(path.read_text(encoding="utf-8")).items():
        if key not in environ:
            environ[key] = value
            applied[key] = value
    return applied


def find_env_file(start_points: list[Path] | None = None) -> Path | None:
    """Locate the repo-root .env: KB_ENV_FILE, else the nearest ancestor with compose.yaml + .env."""
    explicit = os.environ.get("KB_ENV_FILE")
    if explicit:
        return Path(explicit).expanduser()
    for start in start_points or [Path(__file__).resolve(), Path.cwd()]:
        for directory in [start, *start.parents]:
            if (directory / "compose.yaml").is_file() and (directory / ".env").is_file():
                return directory / ".env"
    return None


@dataclass(frozen=True)
class Settings:
    database_url: str
    litellm_base_url: str
    litellm_key: str | None
    embed_model: str  # LiteLLM alias used for requests
    embed_model_name: str  # underlying model, stored in chunks.embedding_model
    embed_dim: int
    embed_batch_size: int
    embed_timeout: float

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Settings":
        return cls(
            database_url=env.get("KB_DATABASE_URL") or _default_database_url(env),
            litellm_base_url=(env.get("LITELLM_BASE_URL") or "http://127.0.0.1:4000").rstrip("/"),
            litellm_key=env.get("KB_LITELLM_KEY") or None,
            embed_model=env.get("KB_EMBED_MODEL") or "embed-local",
            embed_model_name=env.get("KB_EMBED_MODEL_NAME") or "qwen3-embedding:0.6b",
            embed_dim=int(env.get("KB_EMBED_DIM") or 1024),
            embed_batch_size=int(env.get("KB_EMBED_BATCH") or 16),
            embed_timeout=float(env.get("KB_EMBED_TIMEOUT") or 300),
        )


def _default_database_url(env: Mapping[str, str]) -> str:
    host = env.get("KB_DB_HOST") or "127.0.0.1"
    port = env.get("PG_HOST_PORT") or "5432"
    password = env.get("AIOS_DB_PASSWORD")
    auth = f"aios:{quote(password, safe='')}" if password else "aios"
    return f"postgresql://{auth}@{host}:{port}/aios"


def load_settings(env_file: Path | None = None) -> Settings:
    path = env_file or find_env_file()
    if path and path.is_file():
        load_dotenv(path)
    return Settings.from_env(os.environ)
