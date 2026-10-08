"""Keep the Railway kit in step with the local stack (python3 + PyYAML).

1. Every environment variable compose gives a service that also runs on Railway exists in its variables.env
   (except the documented compose-only ones), so a variable added locally is not forgotten in the cloud.
2. Image pins in infra/railway/*/Dockerfile match compose.yaml / .env.example / decision/Dockerfile.
3. The Hermes config rehost leaves no compose hostname behind.
4. A skill script's AIOS_*_URL default that names a compose host (it runs in sandbox SSH sessions) is set on the
   Railway sandbox, where compose hostnames do not resolve.
"""

import re
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

RAILWAY = Path(__file__).resolve().parents[1]
REPO = RAILWAY.parents[1]
sys.path.insert(0, str(RAILWAY / "lib"))
import manifest  # noqa: E402

# compose service -> Railway service, and compose-only variables with the reason they are not on Railway
PAIRS = {"postgres": "postgres", "litellm": "litellm", "decision": "decision", "knowledge": "knowledge",
         "hermes": "hermes"}
COMPOSE_ONLY = {
    "hermes": {"HERMES_UID", "HERMES_GID"},  # host UID mapping for the bind mount; Railway volumes are chowned at boot
    "sandbox": {"GITHUB_TOKEN"},  # needs the /run/aios tmpfs; Railway has none and the entrypoint refuses disk
}
SOFT = {"sandbox"}  # sandbox parity is reported, not enforced (Railway has no tmpfs/caps; see README)


def compose_services() -> dict:
    return dict(yaml.safe_load((REPO / "compose.yaml").read_text(encoding="utf-8"))["services"])


def env_keys(svc: dict) -> set[str]:
    env = svc.get("environment") or {}
    if isinstance(env, list):
        return {e.split("=", 1)[0] for e in env}
    return set(env)


def image_of(dockerfile: Path, stage: int = 0) -> str:
    froms = re.findall(r"^FROM\s+(\S+)", dockerfile.read_text(encoding="utf-8"), re.M)
    return froms[stage].split("@", 1)[0]


def main() -> int:
    failures: list[str] = []
    notes: list[str] = []
    m = manifest.load()
    railway_vars = {s.name: s.var_names for s in m.services}
    compose = compose_services()

    for c_name, r_name in {**PAIRS, **{s: s for s in SOFT}}.items():
        if c_name not in compose:
            (notes if c_name in SOFT else failures).append(f"compose has no service {c_name!r}")
            continue
        missing = env_keys(compose[c_name]) - railway_vars.get(r_name, set()) - COMPOSE_ONLY.get(c_name, set())
        if missing:
            msg = f"{r_name}/variables.env lacks {sorted(missing)} (set in compose for {c_name})"
            (notes if c_name in SOFT else failures).append(msg)

    env_example = dict(re.findall(r"^([A-Z0-9_]+)=(.*)$", (REPO / ".env.example").read_text(encoding="utf-8"), re.M))
    pins = [
        ("hermes", image_of(RAILWAY / "hermes/Dockerfile"),
         f"nousresearch/hermes-agent:{env_example.get('HERMES_TAG', '?')}"),
        ("litellm", image_of(RAILWAY / "litellm/Dockerfile"), compose["litellm"]["image"]),
        ("postgres", image_of(RAILWAY / "postgres/Dockerfile"), compose["postgres"]["image"]),
        ("decision build", image_of(RAILWAY / "decision/Dockerfile", 0), image_of(REPO / "decision/Dockerfile", 0)),
        ("decision runtime", image_of(RAILWAY / "decision/Dockerfile", 1), image_of(REPO / "decision/Dockerfile", 1)),
    ]
    hermes_compose = compose["hermes"]["image"].replace("${HERMES_TAG:-", "").rstrip("}")
    pins.append(("hermes (compose default)", image_of(RAILWAY / "hermes/Dockerfile"), hermes_compose))
    for what, ours, theirs in pins:
        if ours != theirs:
            failures.append(f"pin drift for {what}: infra/railway has {ours}, local stack has {theirs}")

    with tempfile.TemporaryDirectory() as tmp:
        for name in ("config.yaml", "profiles.yaml"):
            src = REPO / "config/hermes" / name
            if not src.is_file():
                continue
            dst = Path(tmp) / name
            dst.write_bytes(src.read_bytes())
            subprocess.run([sys.executable, str(RAILWAY / "hermes/rehost.py"), str(dst)], check=True,
                           capture_output=True)
            text = dst.read_text(encoding="utf-8")
            left = re.findall(r"https?://(?:litellm|knowledge|decision|sandbox|edge|hermes)(?=[:/\"'\s]|$)", text)
            left += re.findall(r"ssh_host:\s*(?:sandbox|litellm)\s*$", text, re.M)
            if left:
                failures.append(f"rehost left compose hostnames in {name}: {left}")
            yaml.safe_load(text)

    sandbox_vars = railway_vars.get("sandbox", set())
    url_default = re.compile(r"\$\{(AIOS_[A-Z0-9_]+_URL):-https?://([A-Za-z0-9.-]+)[:/}]")
    for script in sorted((REPO / "skills").glob("**/scripts/*.sh")):
        for var, host in url_default.findall(script.read_text(encoding="utf-8")):
            if "." not in host and host != "localhost" and var not in sandbox_vars:
                failures.append(f"sandbox/variables.env lacks {var}: {script.relative_to(REPO)} defaults to the "
                                f"compose host {host!r}")

    documented = (RAILWAY / "variables.md").read_text(encoding="utf-8")
    prefixes = tuple(p + "_" for p in re.findall(r"`([A-Z0-9_]+)_\*`", documented))
    undocumented = sorted(k for k in env_example if k not in documented and not k.startswith(prefixes))
    if undocumented:
        notes.append(f"variables.md does not map .env.example variable(s) {undocumented}")

    for n in notes:
        print(f"note: {n}")
    for f in failures:
        print(f"FAIL {f}")
    if not failures:
        print(f"ok   compose parity ({', '.join(PAIRS)}), image pins, hermes rehost, skill URL defaults")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
