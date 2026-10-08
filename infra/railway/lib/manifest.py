"""Railway manifest for the AIOS control plane: load, validate, print the plan, emit Railway IaC.

Sources (all under infra/railway/): services.json (topology), shared.env (shared variable names),
<dir>/railway.json (build/deploy, Railway config-as-code schema) and <dir>/variables.env (service variables).
Offline and read-only: nothing here talks to Railway. Stdlib only.

    python3 infra/railway/lib/manifest.py plan  [--env-file .env]
    python3 infra/railway/lib/manifest.py check
    python3 infra/railway/lib/manifest.py iac   [--repo owner/name] [--branch main]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent.parent
RAILWAY_DIR = Path(os.environ.get("AIOS_RAILWAY_DIR") or HERE.parent)  # override: tests on a modified copy

REF = re.compile(r"\$\{\{\s*([A-Za-z0-9_-]+)(?:\.([A-Za-z0-9_]+))?\s*\}\}")
NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
DNS_LABEL = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
SHARED_LINE = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*?)(?:\s+#\s*(secret|plain)\b:?\s*(.*))?$")
SECRET_NAME = re.compile(r"(PASSWORD|SECRET|TOKEN|AUTHKEY|_KEY|_KEY_B64|_KEY_ID)$|^NTFY_URL$")
URL_PASSWORD = re.compile(r"://[^:/@]+:([^@]+)@")

# Variables Railway provides to every service (docs.railway.com/variables/reference) plus PORT.
RAILWAY_PROVIDED = {
    "RAILWAY_PUBLIC_DOMAIN", "RAILWAY_PRIVATE_DOMAIN", "RAILWAY_TCP_PROXY_DOMAIN", "RAILWAY_TCP_PROXY_PORT",
    "RAILWAY_TCP_APPLICATION_PORT", "RAILWAY_PROJECT_NAME", "RAILWAY_PROJECT_ID", "RAILWAY_ENVIRONMENT_NAME",
    "RAILWAY_ENVIRONMENT_ID", "RAILWAY_SERVICE_NAME", "RAILWAY_SERVICE_ID", "RAILWAY_REPLICA_ID",
    "RAILWAY_REPLICA_REGION", "RAILWAY_DEPLOYMENT_ID", "RAILWAY_SNAPSHOT_ID", "RAILWAY_VOLUME_NAME",
    "RAILWAY_VOLUME_MOUNT_PATH", "PORT",
}
DATABASE_VARS = {
    "redis": RAILWAY_PROVIDED | {"REDIS_PASSWORD", "REDIS_PUBLIC_URL", "REDIS_URL", "REDISHOST", "REDISPASSWORD",
                                 "REDISPORT", "REDISUSER"},
    "postgres": RAILWAY_PROVIDED | {"DATABASE_PUBLIC_URL", "DATABASE_URL", "PGDATA", "PGDATABASE", "PGHOST",
                                    "PGPASSWORD", "PGPORT", "PGUSER", "POSTGRES_DB", "POSTGRES_PASSWORD",
                                    "POSTGRES_USER"},
}
BUCKET_VARS = {"BUCKET", "SECRET_ACCESS_KEY", "ACCESS_KEY_ID", "REGION", "ENDPOINT", "RAILWAY_BUCKET_NAME",
               "RAILWAY_BUCKET_ID", "RAILWAY_PROJECT_NAME", "RAILWAY_PROJECT_ID", "RAILWAY_ENVIRONMENT_NAME",
               "RAILWAY_ENVIRONMENT_ID"}
DRIFT_IGNORE = {"TZ"}  # a shared variable for app services; postgres stays UTC on purpose, as in compose
BUILD_KEYS = {"builder", "watchPatterns", "buildCommand", "dockerfilePath", "railpackVersion"}
DEPLOY_KEYS = {"startCommand", "preDeployCommand", "preDeployTimeoutSeconds", "numReplicas", "healthcheckPath",
               "healthcheckTimeout", "sleepApplication", "restartPolicyType", "restartPolicyMaxRetries",
               "cronSchedule", "region", "requiredMountPath", "overlapSeconds", "drainingSeconds"}


@dataclass
class Var:
    name: str
    value: str

    @property
    def refs(self) -> list[tuple[str | None, str]]:
        out = []
        for ns, var in REF.findall(self.value):
            out.append((None, ns) if not var else (ns, var))
        return out


@dataclass
class Service:
    name: str
    dir: str
    root: str
    port: int | None
    role: str
    volume: dict | None
    config: dict = field(default_factory=dict)
    variables: list[Var] = field(default_factory=list)

    @property
    def var_names(self) -> set[str]:
        return {v.name for v in self.variables}


@dataclass
class Shared:
    name: str
    default: str
    kind: str
    note: str


@dataclass
class Manifest:
    project: str
    branch: str
    services: list[Service]
    databases: list[dict]
    buckets: list[dict]
    shared: dict[str, Shared]


def parse_env_lines(text: str) -> list[Var]:
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, value = line.partition("=")
        if not sep:
            raise ValueError(f"not NAME=VALUE: {raw!r}")
        out.append(Var(name.strip(), value.strip()))
    return out


def parse_shared(text: str) -> dict[str, Shared]:
    out: dict[str, Shared] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = SHARED_LINE.match(line)
        if not m:
            raise ValueError(f"shared.env: cannot parse {raw!r}")
        name, default, kind, note = m.groups()
        out[name] = Shared(name, default.strip(), kind or "plain", (note or "").strip())
    return out


def read_dotenv(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.is_file():
        return env
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.split(" #", 1)[0].strip().strip('"').strip("'")
    return env


def load(root: Path = RAILWAY_DIR) -> Manifest:
    spec = json.loads((root / "services.json").read_text(encoding="utf-8"))
    services = []
    for s in spec["services"]:
        svc = Service(s["name"], s["dir"], s.get("rootDirectory", "/"), s.get("port"), s.get("role", ""),
                      s.get("volume"))
        cfg = root / s["dir"] / "railway.json"
        if cfg.is_file():
            svc.config = json.loads(cfg.read_text(encoding="utf-8"))
        env = root / s["dir"] / "variables.env"
        if env.is_file():
            svc.variables = parse_env_lines(env.read_text(encoding="utf-8"))
        services.append(svc)
    shared = parse_shared((root / "shared.env").read_text(encoding="utf-8"))
    return Manifest(spec.get("project", "aios"), spec.get("branch", "main"), services,
                    spec.get("databases", []), spec.get("buckets", []), shared)


def is_secret_name(name: str) -> bool:
    return bool(SECRET_NAME.search(name))


def check(m: Manifest, root: Path = RAILWAY_DIR, repo: Path = REPO_ROOT) -> tuple[list[str], list[str]]:
    """Return (errors, warnings)."""
    errors: list[str] = []
    warnings: list[str] = []
    names = [s.name for s in m.services] + [d["name"] for d in m.databases] + [b["name"] for b in m.buckets]
    for n in names:
        if not DNS_LABEL.match(n):
            errors.append(f"{n}: resource names must be lowercase DNS labels (private DNS is <name>.railway.internal)")
    for n in {n for n in names if names.count(n) > 1}:
        errors.append(f"{n}: duplicate resource name")
    by_name = {s.name: s for s in m.services}
    db_engine = {d["name"]: d["engine"] for d in m.databases}
    bucket_names = {b["name"] for b in m.buckets}
    used_shared: set[str] = set()

    for s in m.services:
        where = f"{s.name}"
        if not (root / s.dir).is_dir():
            errors.append(f"{where}: missing directory infra/railway/{s.dir}")
            continue
        if not s.config:
            errors.append(f"{where}: missing railway.json")
            continue
        build, deploy = s.config.get("build", {}), s.config.get("deploy", {})
        if s.config.get("$schema") != "https://railway.com/railway.schema.json":
            errors.append(f"{where}: railway.json must declare the Railway $schema")
        for k in sorted(set(build) - BUILD_KEYS):
            errors.append(f"{where}: unknown build key {k!r}")
        for k in sorted(set(deploy) - DEPLOY_KEYS):
            errors.append(f"{where}: unknown deploy key {k!r}")
        if build.get("builder") != "DOCKERFILE":
            errors.append(f"{where}: builder must be DOCKERFILE")
        root_dir = repo / s.root.lstrip("/")
        dockerfile = root_dir / build.get("dockerfilePath", "Dockerfile")
        if not dockerfile.is_file():
            # a component's own Dockerfile may still be in flight; ours (infra/railway/*) must exist
            owned = dockerfile.resolve().is_relative_to(root.resolve())
            (errors if owned else warnings).append(
                f"{where}: Dockerfile not found at {dockerfile.relative_to(repo)}"
                + ("" if owned else " (component not in the tree yet)"))
        mount = deploy.get("requiredMountPath")
        vol_path = (s.volume or {}).get("mountPath")
        if mount and mount != vol_path:
            errors.append(f"{where}: requiredMountPath {mount} has no matching volume in services.json")
        if vol_path and mount != vol_path:
            warnings.append(f"{where}: volume {vol_path} is not enforced with deploy.requiredMountPath")
        if deploy.get("cronSchedule") and deploy.get("restartPolicyType") == "ALWAYS":
            errors.append(f"{where}: a cron service must exit; restartPolicyType ALWAYS would loop it")
        if not s.variables:
            warnings.append(f"{where}: no variables.env")
        if s.name == "postgres" and {"PGHOST", "PGHOSTADDR"} & s.var_names:
            errors.append(f"{where}: never set PGHOST/PGHOSTADDR on the postgres service itself: its executable "
                          "initdb scripts would dial TCP while the first-boot server listens on the socket only")

        seen: set[str] = set()
        for v in s.variables:
            if not NAME.match(v.name):
                errors.append(f"{where}: invalid variable name {v.name!r}")
            if v.name in seen:
                errors.append(f"{where}: {v.name} defined twice")
            seen.add(v.name)
            literal = REF.sub("", v.value)
            if is_secret_name(v.name) and literal and not v.value.startswith("/") and not v.refs:
                errors.append(f"{where}: {v.name} holds a literal value; secrets must be ${{{{shared.*}}}} references")
            pw = URL_PASSWORD.search(v.value)
            if pw and not REF.fullmatch(pw.group(1)):
                errors.append(f"{where}: {v.name} embeds a literal password; use ${{{{shared.*}}}}")
            for ns, var in v.refs:
                if ns is None:
                    if var not in RAILWAY_PROVIDED and var not in s.var_names:
                        errors.append(f"{where}: {v.name} references unknown ${{{{{var}}}}}")
                elif ns == "shared":
                    used_shared.add(var)
                    if var not in m.shared:
                        errors.append(f"{where}: {v.name} references ${{{{shared.{var}}}}} missing from shared.env")
                elif ns in by_name:
                    if var not in RAILWAY_PROVIDED and var not in by_name[ns].var_names:
                        errors.append(f"{where}: {v.name} references {ns}.{var}, which {ns} does not define")
                elif ns in db_engine:
                    if var not in DATABASE_VARS.get(db_engine[ns], RAILWAY_PROVIDED):
                        errors.append(f"{where}: {v.name} references {ns}.{var}, not a {db_engine[ns]} variable")
                elif ns in bucket_names:
                    if var not in BUCKET_VARS:
                        errors.append(f"{where}: {v.name} references {ns}.{var}, not a bucket variable")
                else:
                    errors.append(f"{where}: {v.name} references unknown resource {ns!r}")

    for sh in m.shared.values():
        if sh.kind == "secret" and sh.default:
            errors.append(f"shared.env: secret {sh.name} must not carry a value in git")
        if sh.name not in used_shared:
            warnings.append(f"shared.env: {sh.name} is not referenced by any service")
    return errors, warnings


def _fmt_deploy(d: dict) -> str:
    parts = [f"restart={d.get('restartPolicyType') or 'default'}"]
    if d.get("restartPolicyMaxRetries"):
        parts[-1] += f"x{d['restartPolicyMaxRetries']}"
    parts.append(f"replicas={d.get('numReplicas') or 1}")
    parts.append(f"healthcheck={d.get('healthcheckPath') or '-'}"
                 + (f" ({d['healthcheckTimeout']}s)" if d.get("healthcheckPath") and d.get("healthcheckTimeout") else ""))
    if d.get("cronSchedule"):
        parts.append(f"cron='{d['cronSchedule']}' (UTC)")
    return " ".join(parts)


def plan(m: Manifest, env_file: Path) -> str:
    dotenv = read_dotenv(env_file)
    out = [f"Railway plan for project {m.project!r} (offline: nothing is created or changed)", ""]
    out.append("Managed resources")
    for d in m.databases:
        out.append(f"  {d['name']:<14} {d['engine']} template   {d.get('role', '')}")
    for b in m.buckets:
        out.append(f"  {b['name']:<14} bucket ({b.get('region', '?')})   {b.get('role', '')}")
    out.append("")
    out.append(f"Services (GitHub source, branch {m.branch}; create in this order)")
    for s in m.services:
        b, d = s.config.get("build", {}), s.config.get("deploy", {})
        out.append(f"  {s.name}  - {s.role}")
        out.append(f"      build   root={s.root} dockerfile={b.get('dockerfilePath', 'Dockerfile')}")
        out.append(f"      deploy  {_fmt_deploy(d)}")
        if s.port:
            out.append(f"      private {s.name}.railway.internal:{s.port} (no public domain)")
        if s.volume:
            v = s.volume
            backups = ",".join(v.get("backups", [])) or "none"
            out.append(f"      volume  {v['name']} -> {v['mountPath']} ({v.get('sizeMB', '?')} MB, backups {backups})")
        out.append(f"      variables ({len(s.variables)}):")
        for v in s.variables:
            out.append(f"        {v.name}={v.value}")
    out.append("")
    out.append("Shared variables (Project Settings -> Shared Variables)")
    missing = []
    for sh in m.shared.values():
        local = dotenv.get(sh.name, "")
        if sh.kind == "secret":
            state = ".env: set" if local else ".env: EMPTY"
            if not local:
                missing.append(sh.name)
        else:
            state = f"default {sh.default or '(empty)'}" + (f"; .env: {local}" if local and local != sh.default else "")
        out.append(f"  {sh.kind:<6} {sh.name:<26} {state}   {sh.note}")
    drift = []
    for s in m.services:
        for v in s.variables:
            if v.refs or is_secret_name(v.name) or v.name in DRIFT_IGNORE or not dotenv.get(v.name):
                continue
            if dotenv[v.name] != v.value:
                drift.append(f"  {s.name}.{v.name}: .env={dotenv[v.name]}  railway={v.value}")
    if drift:
        out += ["", "Drift (.env differs from the Railway literal; adjust variables.env if the .env value is intended)"]
        out += drift
    secrets = [sh.name for sh in m.shared.values() if sh.kind == "secret" and dotenv.get(sh.name)]
    out += ["", "Paste secrets without printing them anywhere else (Shared Variables -> Raw Editor):",
            f"  grep -E '^({'|'.join(secrets)})=' .env | xclip -selection clipboard   # or wl-copy"
            if secrets else "  (no secrets in .env)"]
    if missing:
        out.append(f"  not in .env (set by hand if used): {', '.join(missing)}")
    return "\n".join(out)


def _ident(prefix: str, name: str) -> str:
    return prefix + "".join(p.capitalize() for p in re.split(r"[^A-Za-z0-9]+", name) if p)


def _ts(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def iac(m: Manifest, repo: str, branch: str) -> str:
    """Railway IaC (.railway/railway.ts, `railway/iac` SDK 3.x) equivalent to the manifest."""
    lines = [
        "// GENERATED by `infra/scripts/railway-plan.sh --iac` from infra/railway/{services.json,*/railway.json,",
        "// */variables.env}. Edit those files and regenerate. Save as .railway/railway.ts, run `railway config plan`",
        "// (read-only) and review before `railway config apply`. ${{shared.*}} variables are referenced, not",
        "// defined here: create them first (Project Settings -> Shared Variables). No secret values in this file.",
        'import { bucket, defineRailway, github, project, redis, service, volume } from "railway/iac";',
        "",
        f"const REPO = {_ts(repo)};",
        f"const BRANCH = {_ts(branch)};",
        "",
        "export default defineRailway(() => {",
    ]
    resources: list[str] = []
    for d in m.databases:
        if d["engine"] != "redis":
            raise ValueError(f"unsupported database engine {d['engine']!r}")
        ident = _ident("db", d["name"])
        lines.append(f"  const {ident} = redis({_ts(d['name'])});")
        resources.append(ident)
    for b in m.buckets:
        ident = _ident("bucket", b["name"])
        cfg = {"region": b["region"]} if b.get("region") else {}
        lines.append(f"  const {ident} = bucket({_ts(b['name'])}, {_ts(cfg)});")
        resources.append(ident)
    for s in m.services:
        if s.volume:
            ident = _ident("vol", s.volume["name"])
            cfg = {"sizeMB": s.volume["sizeMB"]} if s.volume.get("sizeMB") else {}
            lines.append(f"  const {ident} = volume({_ts(s.volume['name'])}, {_ts(cfg)});")
            resources.append(ident)
    for s in m.services:
        ident = _ident("svc", s.name)
        build = {k: v for k, v in s.config.get("build", {}).items() if v is not None}
        deploy = {k: v for k, v in s.config.get("deploy", {}).items() if v is not None}
        lines.append(f"  const {ident} = service({_ts(s.name)}, {{")
        lines.append(f"    source: github(REPO, {{ branch: BRANCH, rootDirectory: {_ts(s.root)} }}),")
        lines.append(f"    build: {_ts(build)},")
        lines.append(f"    deploy: {_ts(deploy)},")
        if s.volume:
            lines.append(f"    volumeMounts: {{ {_ts(s.volume['mountPath'])}: {_ident('vol', s.volume['name'])} }},")
        lines.append("    env: {")
        for v in s.variables:
            lines.append(f"      {_ts(v.name)}: {_ts(v.value)},")
        lines.append("    },")
        lines.append("  });")
        resources.append(ident)
    lines.append("")
    lines.append(f"  return project({_ts(m.project)}, {{ resources: [{', '.join(resources)}] }});")
    lines.append("});")
    return "\n".join(lines) + "\n"


def git_repo_slug(repo: Path = REPO_ROOT) -> str | None:
    try:
        url = subprocess.run(["git", "-C", str(repo), "remote", "get-url", "origin"], capture_output=True,
                             text=True, timeout=5, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", url)
    return m.group(1) if m else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="AIOS Railway manifest (offline)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_plan = sub.add_parser("plan", help="print services, variables and shared variables")
    p_plan.add_argument("--env-file", type=Path, default=REPO_ROOT / ".env")
    sub.add_parser("check", help="validate the manifest; exit 1 on errors")
    p_iac = sub.add_parser("iac", help="print .railway/railway.ts")
    p_iac.add_argument("--repo", help="GitHub owner/name (default: origin remote)")
    p_iac.add_argument("--branch")
    args = ap.parse_args(argv)

    m = load()
    errors, warnings = check(m)
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    for e in errors:
        print(f"error: {e}", file=sys.stderr)
    if errors:
        return 1
    if args.cmd == "plan":
        print(plan(m, args.env_file))
    elif args.cmd == "iac":
        repo = args.repo or git_repo_slug()
        if not repo or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            print("error: pass --repo <owner>/<name> (no GitHub origin remote found)", file=sys.stderr)
            return 2
        print(iac(m, repo, args.branch or m.branch), end="")
    else:
        print(f"ok: {len(m.services)} services, {len(m.databases)} databases, {len(m.buckets)} buckets, "
              f"{len(m.shared)} shared variables")
    return 0


if __name__ == "__main__":
    sys.exit(main())
