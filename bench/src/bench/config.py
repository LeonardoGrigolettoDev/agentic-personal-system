"""Settings from the environment, with the repo-root .env as a fallback (real env vars always win)."""

import os
import re
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

BENCH_HOME = Path(__file__).resolve().parents[2]  # bench/ (editable install, also in the image)
REPO_ROOT = BENCH_HOME.parent

PROFILE_KEY_PREFIX = "HERMES_API_KEY_"
_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def parse_dotenv(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        m = _LINE.match(raw)
        if not m:
            continue
        key, value = m.groups()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        values[key] = value
    return values


def load_dotenv(environ: MutableMapping[str, str] | None = None) -> Path | None:
    """Fill missing variables from BENCH_ENV_FILE or the nearest ancestor holding compose.yaml + .env."""
    environ = os.environ if environ is None else environ
    path: Path | None = None
    if explicit := environ.get("BENCH_ENV_FILE"):
        path = Path(explicit).expanduser()
    else:
        for start in (BENCH_HOME, Path.cwd()):
            for directory in (start, *start.parents):
                if (directory / "compose.yaml").is_file() and (directory / ".env").is_file():
                    path = directory / ".env"
                    break
            if path:
                break
    if path is None or not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for key, value in parse_dotenv(text).items():
        environ.setdefault(key, value)
    return path


@dataclass(frozen=True)
class SSHTarget:
    host: str
    user: str = "agent"
    port: int = 22
    key: str | None = None
    known_hosts: str | None = None


@dataclass(frozen=True)
class Settings:
    hermes_url: str
    hermes_api_key: str
    decision_url: str
    decision_api_key: str
    database_url: str | None
    litellm_url: str
    litellm_key: str
    judge_model: str
    workspace: str | None
    task_timeout: float
    poll_interval: float
    http_timeout: float
    tasks_dir: Path
    fixtures_dir: Path
    routing_file: Path
    agents_dir: Path
    ssh: SSHTarget | None = None
    profile_keys: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Settings":
        home = Path(env.get("BENCH_HOME") or BENCH_HOME)
        ssh = None
        if host := env.get("TERMINAL_SSH_HOST"):
            ssh = SSHTarget(host=host, user=env.get("TERMINAL_SSH_USER") or "agent",
                            port=int(env.get("TERMINAL_SSH_PORT") or 22), key=env.get("TERMINAL_SSH_KEY") or None,
                            known_hosts=env.get("BENCH_SSH_KNOWN_HOSTS") or None)
        profile_keys = {k[len(PROFILE_KEY_PREFIX):].lower(): v for k, v in env.items()
                        if k.startswith(PROFILE_KEY_PREFIX) and v}
        return cls(
            hermes_url=(env.get("AIOS_HERMES_URL") or "http://127.0.0.1:8642").rstrip("/"),
            hermes_api_key=env.get("HERMES_API_KEY") or "",
            decision_url=(env.get("AIOS_DECISION_URL") or "http://127.0.0.1:8090").rstrip("/"),
            decision_api_key=env.get("DECISION_API_KEY") or "",
            database_url=env.get("BENCH_DATABASE_URL") or _default_database_url(env),
            litellm_url=(env.get("LITELLM_BASE_URL") or "http://127.0.0.1:4000").rstrip("/"),
            litellm_key=env.get("BENCH_LITELLM_KEY") or "",
            judge_model=env.get("BENCH_JUDGE_MODEL") or "tier5-sonnet",
            workspace=env.get("BENCH_WORKSPACE") or None,
            task_timeout=float(env.get("BENCH_TASK_TIMEOUT") or 900),
            poll_interval=float(env.get("BENCH_POLL_INTERVAL") or 2),
            http_timeout=float(env.get("BENCH_HTTP_TIMEOUT") or 30),
            tasks_dir=Path(env.get("BENCH_TASKS_DIR") or home / "tasks"),
            fixtures_dir=Path(env.get("BENCH_FIXTURES_DIR") or home / "fixtures"),
            routing_file=Path(env.get("AIOS_ROUTING_FILE") or REPO_ROOT / "config" / "routing.yaml"),
            agents_dir=Path(env.get("AIOS_AGENTS_DIR") or REPO_ROOT / "agents"),
            ssh=ssh,
            profile_keys=profile_keys,
        )


def _default_database_url(env: Mapping[str, str]) -> str | None:
    """Host-side default: the compose postgres published on 127.0.0.1:PG_HOST_PORT."""
    password = env.get("AIOS_DB_PASSWORD")
    if not password:
        return None
    return f"postgresql://aios:{quote(password, safe='')}@127.0.0.1:{env.get('PG_HOST_PORT') or 5432}/aios"
