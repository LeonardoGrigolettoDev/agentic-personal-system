"""Point the Hermes config templates at Railway private DNS names.

config/hermes/*.yaml address services by their compose names (http://litellm:4000, ssh_host: sandbox, ...),
which do not resolve on Railway. The image build rewrites its own copy so the repo files stay compose-shaped:
    python rehost.py config.yaml profiles.yaml
Every URL whose host is a compose service, and every `ssh_host`/`host` value naming one, is mapped below.
"""

import re
import sys
from pathlib import Path

import yaml

SUFFIX = ".railway.internal"
# compose name -> (railway host, port override). The edge worker stays on the ThinkPad behind edge-gw:8093.
HOSTS: dict[str, tuple[str, str | None]] = {
    "litellm": ("litellm", None),
    "decision": ("decision", None),
    "knowledge": ("knowledge", None),
    "sandbox": ("sandbox", None),
    "hermes": ("hermes", None),
    "edge": ("edge-gw", "8093"),
}
URL = re.compile(r"^(?P<scheme>https?://)(?P<host>[a-z][a-z0-9-]*)(?::(?P<port>\d+))?(?P<rest>[/?#].*)?$")
HOST_KEYS = {"ssh_host", "host"}


def rehost_value(value: str, key: str | None) -> str:
    if key in HOST_KEYS and value in HOSTS:
        return HOSTS[value][0] + SUFFIX
    m = URL.match(value)
    if not m or m["host"] not in HOSTS:
        return value
    host, port_override = HOSTS[m["host"]]
    port = port_override or m["port"]
    return f"{m['scheme']}{host}{SUFFIX}{':' + port if port else ''}{m['rest'] or ''}"


def rehost(node, key: str | None = None):
    if isinstance(node, dict):
        return {k: rehost(v, k) for k, v in node.items()}
    if isinstance(node, list):
        return [rehost(v, key) for v in node]
    if isinstance(node, str):
        return rehost_value(node, key)
    return node


def main(paths: list[str]) -> int:
    if not paths:
        print("usage: rehost.py <file.yaml>...", file=sys.stderr)
        return 2
    for p in map(Path, paths):
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        p.write_text(yaml.safe_dump(rehost(data), sort_keys=False, allow_unicode=True, width=120), encoding="utf-8")
        print(f"rehosted {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
