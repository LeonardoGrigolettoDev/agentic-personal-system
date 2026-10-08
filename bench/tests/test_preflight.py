"""`requires` probes: run where the agent works, send EDGE_API_KEY the way Hermes does, cached per run."""

import os
import shutil
import stat

import pytest
from stubs import _Base, serve

from bench.config import SSHTarget
from bench.preflight import Preflight
from bench.workspace import LocalWorkspace, SSHWorkspace

pytestmark = pytest.mark.skipif(shutil.which("curl") is None, reason="needs curl")

# ssh test double: like tests/test_workspace_config.py, plus FAKE_SSH_DROP=<VAR> to emulate an sshd without AcceptEnv
FAKE_SSH = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_SSH_LOG"
[ -n "${FAKE_SSH_DROP:-}" ] && unset "$FAKE_SSH_DROP"
while [ $# -gt 0 ]; do
  case "$1" in -p|-o|-i) shift 2 ;; *@*) shift; break ;; *) shift ;; esac
done
exec bash -c "$*"
"""


class Health(_Base):
    def do_GET(self):
        self.send_json(200 if self.path == "/healthz" else 404, {"status": "ok"})


@pytest.fixture
def edge(monkeypatch):
    with serve(Health, None) as url:
        monkeypatch.setenv("AIOS_EDGE_URL", url)
        yield url


@pytest.fixture
def fake_ssh(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    ssh = bindir / "ssh"
    ssh.write_text(FAKE_SSH)
    ssh.chmod(ssh.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_SSH_LOG", str(tmp_path / "ssh.log"))
    monkeypatch.delenv("EDGE_API_KEY", raising=False)
    return tmp_path / "ssh.log"


def test_edge_probe_passes_when_the_key_arrives_and_edge_answers(edge, fake_ssh):
    pf = Preflight(SSHWorkspace(SSHTarget(host="sandbox"), "/workspace/bench"))
    assert pf.unmet(("edge",)) is None
    assert "SendEnv=EDGE_API_KEY" in fake_ssh.read_text()


def test_edge_probe_fails_when_sshd_drops_the_key(edge, fake_ssh, monkeypatch):
    monkeypatch.setenv("FAKE_SSH_DROP", "EDGE_API_KEY")
    pf = Preflight(SSHWorkspace(SSHTarget(host="sandbox"), "/workspace/bench"))
    reason = pf.unmet(("edge",))
    assert reason.startswith("requires edge: ") and "AcceptEnv EDGE_API_KEY" in reason
    assert pf.unmet(("edge",)) == reason
    assert len(fake_ssh.read_text().splitlines()) == 1  # probed once per run


def test_edge_probe_reports_an_unreachable_edge(tmp_path, monkeypatch):
    monkeypatch.setenv("AIOS_EDGE_URL", "http://127.0.0.1:9")
    reason = Preflight(LocalWorkspace(tmp_path)).unmet(("edge",))
    assert "edge unreachable at http://127.0.0.1:9/healthz" in reason


def test_unknown_requirement_or_no_workspace(tmp_path):
    assert "unknown requirement" in Preflight(LocalWorkspace(tmp_path)).unmet(("gpu",))
    assert "no workspace" in Preflight(None).unmet(("edge",))
    assert Preflight(None).unmet(()) is None
