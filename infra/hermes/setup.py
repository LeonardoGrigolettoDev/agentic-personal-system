"""Configure Hermes for the AI Agent OS — idempotent; run inside the hermes container:

    docker compose --profile agent exec hermes /opt/hermes/.venv/bin/python /opt/aios/bin/setup.py

- chief (default home): config.yaml from config/hermes/config.yaml, SOUL.md from agents/chief, aios plugin
- domain profiles (config/hermes/profiles.yaml): created with `hermes profile create`, same config +
  overrides, their own SOUL.md and the aios plugin; they run on demand as Kanban workers
- cron jobs for the scheduled reviews (prompts in workflows/<name>.md), created once by name

Managed keys are (re)applied on every run; anything else Hermes keeps in config.yaml is preserved.
Paths are env-overridable so the script can be tested outside the container.
"""

import argparse
import copy
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

HOME = Path(os.environ.get("HERMES_HOME", "/opt/data"))
AIOS = Path(os.environ.get("AIOS_ROOT", "/opt/aios"))
HERMES = os.environ.get("HERMES_BIN", "hermes")
PLUGIN_NAME = "aios"
# The gateway multiplexes profiles, and then resolves every profile's secrets - the chief's included - only from
# that profile's .env, never from the container env (verified: LiteLLM got "Bearer no-key", the knowledge MCP 401).
# So the keys Hermes itself reads (model key_env, MCP header, adapters) are synced into each .env here.
PROFILE_SECRETS = ("LITELLM_API_KEY", "KNOWLEDGE_API_KEY")
CHIEF_SECRETS = (*PROFILE_SECRETS, "TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USERS", "HERMES_LANGFUSE_PUBLIC_KEY",
                 "HERMES_LANGFUSE_SECRET_KEY", "HERMES_LANGFUSE_BASE_URL")


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def load_yaml(path: Path) -> dict:
    if not path.is_file():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def write_config(home: Path, managed: dict, dry: bool) -> bool:
    """Apply managed keys over the existing config.yaml (Hermes-added keys survive). Returns changed."""
    path = home / "config.yaml"
    current = load_yaml(path)
    merged = deep_merge(current, managed)
    if merged == current and path.exists():
        return False
    if not dry:
        home.mkdir(parents=True, exist_ok=True)
        if path.exists():
            shutil.copy2(path, path.with_suffix(".yaml.aios-bak"))
        path.write_text(yaml.safe_dump(merged, sort_keys=False, allow_unicode=True, width=120), encoding="utf-8")
    return True


def sync_file(src: Path, dst: Path, dry: bool) -> bool:
    if not src.is_file():
        return False
    if dst.is_file() and dst.read_bytes() == src.read_bytes():
        return False
    if not dry:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
    return True


def link_plugin(home: Path, dry: bool) -> bool:
    """plugins/aios -> the read-only plugin mount (updates with the repo, no copy to drift)."""
    target = AIOS / "plugins" / PLUGIN_NAME
    link = home / "plugins" / PLUGIN_NAME
    if link.is_symlink() and Path(os.readlink(link)) == target:
        return False
    if not dry:
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink() or link.is_file():
            link.unlink()
        elif link.is_dir():
            shutil.rmtree(link)
        link.symlink_to(target, target_is_directory=True)
    return True


def set_env_value(path: Path, key: str, value: str, dry: bool) -> bool:
    """Set KEY=value in a Hermes .env, keeping every other line. Returns changed."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    wanted = f"{key}={value}"
    kept = [ln for ln in lines if not ln.startswith(f"{key}=")]
    if wanted in lines and len(kept) == len(lines) - 1:
        return False
    if not dry:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join([*kept, wanted]) + "\n", encoding="utf-8")
        path.chmod(0o600)
    return True


def without_kanban(config: dict) -> dict:
    """Only the chief orchestrates the board; domain profiles get worker tools from the dispatcher."""
    out = copy.deepcopy(config)
    for platform, names in (out.get("platform_toolsets") or {}).items():
        if isinstance(names, list):
            out["platform_toolsets"][platform] = [n for n in names if n != "kanban"]
    return out


def hermes(*args: str, home: Path = HOME) -> subprocess.CompletedProcess:
    env = {**os.environ, "HERMES_HOME": str(home)}
    return subprocess.run([HERMES, *args], env=env, capture_output=True, text=True, timeout=120)


def ensure_profile(name: str, description: str, dry: bool) -> tuple[Path, bool]:
    home = HOME / "profiles" / name
    if (home / "config.yaml").is_file() or (home / "SOUL.md").is_file():
        return home, False
    if not dry:
        args = ["profile", "create", name, "--no-alias"]
        if description:
            args += ["--description", description]
        proc = hermes(*args)
        if proc.returncode != 0:
            raise SystemExit(f"hermes profile create {name} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return home, True


def existing_cron_names(home: Path) -> dict[str, dict]:
    path = home / "cron" / "jobs.json"
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8") or "{}")
    jobs = data.get("jobs", data) if isinstance(data, dict) else data
    if isinstance(jobs, dict):
        jobs = list(jobs.values())
    return {j.get("name"): j for j in jobs if isinstance(j, dict) and j.get("name")}


def ensure_cron(jobs: list[dict], dry: bool, recreate: bool) -> list[str]:
    done = []
    existing = existing_cron_names(HOME)
    deliver = os.environ.get("AIOS_CRON_DELIVER", "local")
    for job in jobs:
        name = job["name"]
        prompt_file = AIOS / "workflows" / f"{name}.md"
        if not prompt_file.is_file():
            done.append(f"cron {name}: SKIPPED (missing {prompt_file})")
            continue
        if name in existing and not recreate:
            continue
        if dry:
            done.append(f"cron {name}: would {'recreate' if name in existing else 'create'}")
            continue
        if name in existing:
            job_id = existing[name].get("id") or existing[name].get("job_id")
            if job_id:
                hermes("cron", "remove", str(job_id))
        args = ["cron", "create", job["schedule"], prompt_file.read_text(encoding="utf-8").strip(),
                "--name", name, "--deliver", deliver]
        for skill in job.get("skills", []):
            args += ["--skill", skill]
        proc = hermes(*args)
        status = "ok" if proc.returncode == 0 else f"FAILED: {(proc.stderr or proc.stdout).strip()[:200]}"
        done.append(f"cron {name} ({job['schedule']}): {status}")
    return done


def main() -> int:
    if hasattr(os, "geteuid") and os.geteuid() == 0 and not os.environ.get("AIOS_SETUP_AS_ROOT"):
        # docker exec defaults to root: config.yaml/.env written now would be root-owned (and .env 0600),
        # unreadable to the gateway, which runs as the hermes user
        print("run as the hermes user: docker compose --profile agent exec --user hermes hermes ... "
              "(or `make hermes-setup`); AIOS_SETUP_AS_ROOT=1 overrides", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--recreate-cron", action="store_true", help="re-create cron jobs from workflows/*.md")
    parser.add_argument("--no-cron", action="store_true")
    args = parser.parse_args()

    template = load_yaml(AIOS / "config" / "config.yaml")
    spec = load_yaml(AIOS / "config" / "profiles.yaml")
    if not template:
        print(f"missing {AIOS}/config/config.yaml", file=sys.stderr)
        return 1
    if os.environ.get("HERMES_LANGFUSE_PUBLIC_KEY") and os.environ.get("HERMES_LANGFUSE_SECRET_KEY"):
        enabled = template.setdefault("plugins", {}).setdefault("enabled", [])
        if "observability/langfuse" not in enabled:
            enabled.append("observability/langfuse")  # bundled Hermes plugin: traces per turn/LLM call/tool
    report = []

    changed = write_config(HOME, template, args.dry_run)
    report.append(f"chief config.yaml: {'updated' if changed else 'unchanged'}")
    synced = [k for k in CHIEF_SECRETS if os.environ.get(k) and set_env_value(HOME / ".env", k, os.environ[k], args.dry_run)]
    report.append(f"chief secrets: {', '.join(synced) or 'unchanged'}")
    soul = sync_file(AIOS / "agents/chief/SOUL.md", HOME / "SOUL.md", args.dry_run)
    report.append(f"chief SOUL.md: {'updated' if soul else 'unchanged'}")
    report.append(f"chief plugin link: {'created' if link_plugin(HOME, args.dry_run) else 'ok'}")

    for name, prof in (spec.get("profiles") or {}).items():
        home, created = ensure_profile(name, prof.get("description", ""), args.dry_run)
        managed = without_kanban(deep_merge(template, prof.get("overrides") or {}))
        managed.setdefault("kanban", {})["dispatch_in_gateway"] = False  # only the chief gateway dispatches
        cfg = write_config(home, managed, args.dry_run)
        soul = sync_file(AIOS / f"agents/{name}/SOUL.md", home / "SOUL.md", args.dry_run)
        plugin = link_plugin(home, args.dry_run)
        # /p/<profile>/ on the multiplexed API server authenticates with the profile's own API_SERVER_KEY
        secrets = {k: os.environ[k] for k in PROFILE_SECRETS if os.environ.get(k)}
        if api_key := os.environ.get(f"HERMES_API_KEY_{name.upper()}", ""):
            secrets["API_SERVER_KEY"] = api_key
        synced = [k for k, v in secrets.items() if set_env_value(home / ".env", k, v, args.dry_run)]
        report.append(f"profile {name}: {'created' if created else 'exists'}; config {'updated' if cfg else 'unchanged'}; "
                      f"SOUL {'updated' if soul else 'unchanged'}; plugin {'linked' if plugin else 'ok'}; "
                      f"secrets {', '.join(synced) or 'unchanged'}{'' if api_key else ' (no API key)'}")

    if not args.no_cron:
        report += ensure_cron(spec.get("cron") or [], args.dry_run, args.recreate_cron)

    print("\n".join(report))
    if not args.dry_run:
        print("restart the gateway to load changes: docker compose --profile agent restart hermes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
