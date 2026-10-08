"""JSON, manifest and process helpers for the aios-* sandbox commands. Stdlib only; always run as
`/usr/bin/python3 -I` (common.sh aios_py), never with a python3 from the agent-writable PATH.

  result    read the output tail on stdin, print the check result as ONE JSON line (ASCII-only)
  npm-info  package.json -> test=0|1, deps=0|1, pm=<packageManager name>
  py-info   pyproject.toml -> key=value lines describing how pytest can run (see cmd_py_info)
  watch     stop a running check once nobody can read its result (see cmd_watch)
"""

import argparse
import json
import os
import re
import select
import signal
import stat
import sys
import tomllib

MAX_TAIL_CHARS = 20000  # the line must stay small; the plugin keeps only the last 6000 chars anyway
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
NPM_PLACEHOLDER = "no test specified"
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
RELEASE = r"\d+(?:\.\d+)*"
NON_INDEX_KEYS = {"path", "git", "url", "file"}


def cmd_result(args: argparse.Namespace) -> int:
    tail = ANSI.sub("", sys.stdin.buffer.read().decode("utf-8", errors="replace"))
    if len(tail) > MAX_TAIL_CHARS:
        tail = tail[-MAX_TAIL_CHARS:]
    out = {"exit_code": args.exit_code, "output_tail": tail, "command": args.command, "skipped": args.skipped}
    if args.reason:
        out["reason"] = args.reason
    if args.root:
        out["root"] = args.root
    if args.duration_ms is not None:
        out["duration_ms"] = args.duration_ms
    out["timed_out"] = args.timed_out
    sys.stdout.write(json.dumps(out) + "\n")
    return 0


def cmd_npm_info(args: argparse.Namespace) -> int:
    with open(args.path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("package.json is not an object")
    scripts = data.get("scripts") if isinstance(data.get("scripts"), dict) else {}
    test = scripts.get("test")
    has_test = isinstance(test, str) and bool(test.strip()) and NPM_PLACEHOLDER not in test
    deps = any(data.get(k) for k in ("dependencies", "devDependencies", "optionalDependencies"))
    pm = data.get("packageManager")
    pm_name = pm.split("@", 1)[0].strip() if isinstance(pm, str) else ""
    if not SAFE_NAME.match(pm_name):
        pm_name = ""
    print(f"test={int(has_test)}\ndeps={int(deps)}\npm={pm_name}")
    return 0


def _table(value) -> dict:
    return value if isinstance(value, dict) else {}


def _dist_name(spec) -> str:
    if not isinstance(spec, str):
        return ""
    m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", spec)
    return re.sub(r"[-_.]+", "-", m.group(1)).lower() if m else ""


def _mentions_pytest(specs) -> bool:
    return isinstance(specs, list) and any(_dist_name(s) == "pytest" for s in specs)


def pytest_mode(data: dict) -> str:
    """How `uv run` gets pytest for a PEP 621 project: dev | group:<g> | extra:<e> | none."""
    project = _table(data.get("project"))
    uv = _table(_table(data.get("tool")).get("uv"))
    groups = _table(data.get("dependency-groups"))

    def group_has(name: str, seen: frozenset = frozenset()) -> bool:
        if name in seen:
            return False
        for entry in groups.get(name) or []:
            if _dist_name(entry) == "pytest":
                return True
            if isinstance(entry, dict) and isinstance(entry.get("include-group"), str):
                if group_has(entry["include-group"], seen | {name}):
                    return True
        return False

    # What plain `uv run` installs: project deps, tool.uv.dev-dependencies and the default groups ("dev").
    default_groups = uv.get("default-groups", ["dev"])
    if default_groups == "all":
        default_groups = list(groups)
    if (_mentions_pytest(project.get("dependencies")) or _mentions_pytest(uv.get("dev-dependencies"))
            or any(group_has(g) for g in default_groups if isinstance(g, str))):
        return "dev"
    for name in groups:
        if SAFE_NAME.match(name) and group_has(name):
            return f"group:{name}"
    for name, specs in _table(project.get("optional-dependencies")).items():
        if SAFE_NAME.match(name) and _mentions_pytest(specs):
            return f"extra:{name}"
    return "none"


def _bump(release: str, index: int) -> str:
    parts = [int(p) for p in release.split(".")][: index + 1]
    parts[-1] += 1
    return ".".join(map(str, parts))


def poetry_constraint(constraint) -> str:
    """Poetry version constraint -> PEP 440 specifier ("" = any version, also when it has no PEP 440 form)."""
    if not isinstance(constraint, str) or "|" in constraint:
        return ""
    out = []
    for part in re.split(r"\s*,\s*|\s+(?=[<>=!~^])", constraint.strip()):
        if part in ("", "*"):
            continue
        if m := re.fullmatch(rf"\^\s*({RELEASE})", part):
            parts = m.group(1).split(".")
            first = next((i for i, p in enumerate(parts) if int(p)), len(parts) - 1)
            out.append(f">={m.group(1)},<{_bump(m.group(1), first)}")
        elif m := re.fullmatch(rf"~(?!=)\s*({RELEASE})", part):
            out.append(f">={m.group(1)},<{_bump(m.group(1), min(1, m.group(1).count('.')))}")
        elif re.fullmatch(r"(?:===?|!=|<=?|>=?|~=)\s*\d[A-Za-z0-9.*+!-]*", part):
            out.append(part.replace(" ", ""))
        elif re.fullmatch(rf"{RELEASE}(?:\.\*)?", part):
            out.append(f"=={part}")
        else:
            return ""
    return ",".join(out)


def poetry_requirement(name: str, value) -> str | None:
    """PEP 508 requirement for one Poetry dependency; None when only Poetry can install it (path/git/url)."""
    if isinstance(value, list):  # several constraints selected by markers: any release will do
        return None if any(isinstance(v, dict) and NON_INDEX_KEYS & set(v) for v in value) else name
    extras = ""
    if isinstance(value, dict):
        if NON_INDEX_KEYS & set(value):
            return None
        names = [e for e in value.get("extras") or [] if isinstance(e, str) and SAFE_NAME.match(e)]
        extras = f"[{','.join(names)}]" if names else ""
        value = value.get("version", "*")
    return name + extras + poetry_constraint(value)


def poetry_info(poetry: dict, build: bool) -> list[str]:
    """Poetry (no [project] table): installable = poetry-core can build it, so `--with-editable .` brings
    the main dependencies; everything else (main deps otherwise, non-optional groups always) goes to
    `--with`. `unsupported` names a dependency that is not on an index, which only Poetry can install."""
    installable = build and poetry.get("package-mode", True) is not False
    sections = [] if installable else [poetry.get("dependencies")]
    sections.append(poetry.get("dev-dependencies"))
    sections += [g.get("dependencies") for g in _table(poetry.get("group")).values()
                 if isinstance(g, dict) and not g.get("optional", False)]
    lines = [f"installable={int(installable)}"]
    for section in sections:
        for name, value in _table(section).items():
            if name.lower() == "python" or not SAFE_NAME.match(name):
                continue
            req = poetry_requirement(name, value)
            lines.append(f"with={req}" if req else f"unsupported={name}")
    return lines


def cmd_py_info(args: argparse.Namespace) -> int:
    """table=project|poetry|none|invalid, build=0|1, then mode=<pytest_mode> (project) or the
    poetry_info lines (poetry)."""
    try:
        with open(args.path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        print("table=invalid\nbuild=0")
        return 0
    build = isinstance(data.get("build-system"), dict)
    poetry = _table(_table(data.get("tool")).get("poetry"))
    if isinstance(data.get("project"), dict):
        lines = ["table=project", f"build={int(build)}", f"mode={pytest_mode(data)}"]
    elif poetry:
        lines = ["table=poetry", f"build={int(build)}", *poetry_info(poetry, build)]
    else:
        lines = ["table=none", f"build={int(build)}"]
    print("\n".join(lines))
    return 0


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def cmd_watch(args: argparse.Namespace) -> int:
    """Runs beside a check (stdin /dev/null, stdout/stderr = the check's own). Sends SIGTERM to --pid
    (`timeout`, which signals its whole process group and SIGKILLs it after --kill-after) when stdout or
    stderr loses its reader, or when --parent (aios-check) dies; exits when --pid ends.

    Over SSH a vanished reader is the client going away, e.g. Hermes killing its ssh mux client on a
    terminal timeout. Without a pty sshd sends no SIGHUP, and under ControlMaster the connection's
    sshd-session survives; but sshd closes the session's pipes, which poll() reports as POLLERR (pipe)
    or POLLHUP (socket, tty) even with an empty event mask."""
    poller = select.poll()
    for fd in (1, 2):
        try:
            mode = os.fstat(fd).st_mode
        except OSError:
            continue
        if stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode) or stat.S_ISCHR(mode):
            poller.register(fd, 0)
    while _alive(args.pid):
        if not _alive(args.parent):
            break
        if any(ev & (select.POLLERR | select.POLLHUP) for _, ev in poller.poll(args.interval_ms)):
            break
    else:
        return 0
    try:
        os.kill(args.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="aios_helper")
    sub = parser.add_subparsers(dest="cmd", required=True)
    res = sub.add_parser("result")
    res.add_argument("--exit-code", type=int, required=True)
    res.add_argument("--command", default="")
    res.add_argument("--root", default="")
    res.add_argument("--reason", default="")
    res.add_argument("--duration-ms", type=int)
    res.add_argument("--skipped", action="store_true")
    res.add_argument("--timed-out", action="store_true")
    res.set_defaults(fn=cmd_result)
    npm = sub.add_parser("npm-info")
    npm.add_argument("path")
    npm.set_defaults(fn=cmd_npm_info)
    py = sub.add_parser("py-info")
    py.add_argument("path")
    py.set_defaults(fn=cmd_py_info)
    watch = sub.add_parser("watch")
    watch.add_argument("--pid", type=int, required=True)
    watch.add_argument("--parent", type=int, required=True)
    watch.add_argument("--interval-ms", type=int, default=2000)
    watch.set_defaults(fn=cmd_watch)
    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except (OSError, ValueError) as exc:
        print(f"aios_helper: {exc}", file=sys.stderr)
        sys.exit(1)
