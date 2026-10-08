"""Where fixtures are staged and command checks run: the sandbox over SSH (same TERMINAL_SSH_* target as
Hermes' terminal backend) or, with --local, a directory on this machine."""

import fnmatch
import io
import os
import re
import shlex
import shutil
import signal
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from bench.config import SSHTarget

TAIL = 4000
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$")
JUNK = ("__pycache__", "*.pyc", ".pytest_cache", ".DS_Store")  # never staged even if present locally


def _junk(relative: Path) -> bool:
    return any(fnmatch.fnmatch(part, pattern) for part in relative.parts for pattern in JUNK)


def _anonymous(ti: tarfile.TarInfo) -> tarfile.TarInfo:
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = ""
    return ti


class WorkspaceError(Exception):
    pass


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    output_tail: str
    timed_out: bool = False


class Workspace(Protocol):
    def stage(self, fixture: Path | None, name: str) -> str: ...
    def run(self, workdir: str, command: str, timeout: float) -> CommandResult: ...
    def probe(self, command: str, env: dict[str, str], timeout: float) -> CommandResult: ...
    def remove(self, workdir: str) -> None: ...


def _check_name(name: str) -> str:
    if not _NAME.match(name):
        raise WorkspaceError(f"unsafe workdir name {name!r}")
    return name


def _tail(text: str) -> str:
    return text[-TAIL:].replace("\x00", "")  # Postgres text/jsonb reject NUL


class LocalWorkspace:
    """Fixtures copied under `base` on this machine; commands run with bash in the workdir."""

    def __init__(self, base: Path):
        self.base = base.resolve()

    def stage(self, fixture: Path | None, name: str) -> str:
        target = self.base / _check_name(name)
        if target.exists():
            shutil.rmtree(target)
        self.base.mkdir(parents=True, exist_ok=True)
        if fixture is not None:
            shutil.copytree(fixture, target, symlinks=False, ignore=shutil.ignore_patterns(*JUNK))
        else:
            target.mkdir()
        return str(target)

    def run(self, workdir: str, command: str, timeout: float) -> CommandResult:
        return self._bash(command, timeout, cwd=workdir)

    def probe(self, command: str, env: dict[str, str], timeout: float) -> CommandResult:
        """Run a precondition probe on this machine, `env` added to the bench's own environment."""
        return self._bash(command, timeout, env={**os.environ, **env})

    @staticmethod
    def _bash(command: str, timeout: float, cwd: str | None = None, env: dict[str, str] | None = None) -> CommandResult:
        proc = subprocess.Popen(["bash", "-c", command], cwd=cwd, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, errors="replace", start_new_session=True)
        try:
            out, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            out, _ = proc.communicate()
            return CommandResult(124, _tail((out or "") + f"\n[bench] timed out after {timeout:.0f}s"), True)
        return CommandResult(proc.returncode, _tail(out or ""))

    def remove(self, workdir: str) -> None:
        path = Path(workdir).resolve()
        if path.parent != self.base:
            raise WorkspaceError(f"refusing to remove {path}: not under {self.base}")
        shutil.rmtree(path, ignore_errors=True)


class SSHWorkspace:
    """Fixtures streamed as tar over SSH into `base` inside the sandbox; commands run there under a login shell
    (toolchain PATH) and a remote `timeout`, so nothing outlives the check."""

    def __init__(self, target: SSHTarget, base: str, connect_timeout: int = 10):
        if not PurePosixPath(base).is_absolute() or PurePosixPath(base) == PurePosixPath("/"):
            raise WorkspaceError(f"sandbox workspace must be an absolute, non-root path: {base!r}")
        self.target = target
        self.base = str(PurePosixPath(base))
        self.connect_timeout = connect_timeout

    def _argv(self, remote: str, send_env: tuple[str, ...] = ()) -> list[str]:
        argv = ["ssh", "-p", str(self.target.port), "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
                "-o", f"ConnectTimeout={self.connect_timeout}", "-o", "ServerAliveInterval=30"]
        for name in send_env:
            argv += ["-o", f"SendEnv={name}"]
        if self.target.known_hosts:
            argv += ["-o", f"UserKnownHostsFile={self.target.known_hosts}"]
        if self.target.key:
            argv += ["-i", self.target.key]
        return argv + [f"{self.target.user}@{self.target.host}", remote]

    def _ssh(self, remote: str, timeout: float, data: bytes | None = None,
             env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        argv = self._argv(remote, tuple(sorted(env or {})))
        full_env = {**os.environ, **env} if env else None
        try:
            return subprocess.run(argv, input=data, capture_output=True, timeout=timeout, check=False, env=full_env)
        except subprocess.TimeoutExpired as exc:
            raise WorkspaceError(f"ssh {self.target.host} timed out after {timeout:.0f}s") from exc
        except OSError as exc:
            raise WorkspaceError(f"ssh not runnable: {exc}") from exc

    def _dir(self, name: str) -> str:
        return f"{self.base}/{_check_name(name)}"

    def stage(self, fixture: Path | None, name: str) -> str:
        workdir = self._dir(name)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            if fixture is not None:
                for path in sorted(p for p in fixture.rglob("*") if not _junk(p.relative_to(fixture))):
                    tar.add(path, arcname=str(path.relative_to(fixture)), recursive=False,
                            filter=_anonymous)
        q = shlex.quote(workdir)
        proc = self._ssh(f"set -e; rm -rf {q}; mkdir -p {q}; tar -xzf - -C {q} --no-same-owner", 120, buf.getvalue())
        if proc.returncode != 0:
            raise WorkspaceError(f"staging into {workdir} failed (rc={proc.returncode}): "
                                 f"{proc.stderr.decode(errors='replace').strip()[-300:]}")
        return workdir

    def run(self, workdir: str, command: str, timeout: float) -> CommandResult:
        if not workdir.startswith(self.base + "/"):
            raise WorkspaceError(f"workdir {workdir} is outside {self.base}")
        secs = max(1, int(timeout))
        remote = f"cd {shlex.quote(workdir)} && timeout -k 10 {secs} bash -lc {shlex.quote(command)} 2>&1"
        proc = self._ssh(remote, timeout + 30)
        out = proc.stdout.decode(errors="replace")
        if proc.returncode == 255:  # ssh itself failed: not a verdict on the task
            raise WorkspaceError(f"ssh failed: {proc.stderr.decode(errors='replace').strip()[-300:]}")
        timed_out = proc.returncode == 124
        if timed_out:
            out += f"\n[bench] timed out after {secs}s"
        return CommandResult(proc.returncode, _tail(out), timed_out)

    def probe(self, command: str, env: dict[str, str], timeout: float) -> CommandResult:
        """Run a precondition probe in the sandbox, sending `env` the way Hermes' terminal does (ssh SendEnv): a
        variable the sandbox sshd does not AcceptEnv never arrives, for the probe as for the agent."""
        secs = max(1, int(timeout))
        proc = self._ssh(f"timeout -k 5 {secs} bash -lc {shlex.quote(command)} 2>&1", timeout + 15, env=env)
        if proc.returncode == 255:
            raise WorkspaceError(f"ssh failed: {proc.stderr.decode(errors='replace').strip()[-300:]}")
        return CommandResult(proc.returncode, _tail(proc.stdout.decode(errors="replace")), proc.returncode == 124)

    def remove(self, workdir: str) -> None:
        if not workdir.startswith(self.base + "/"):
            raise WorkspaceError(f"refusing to remove {workdir}: not under {self.base}")
        self._ssh(f"rm -rf {shlex.quote(workdir)}", 60)
