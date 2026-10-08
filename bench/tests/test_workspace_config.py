import io
import json
import logging
import os
import stat

import pytest
from conftest import FIXTURES_DIR

from bench import logs
from bench.config import Settings, SSHTarget, load_dotenv, parse_dotenv
from bench.store import strip_nul
from bench.workspace import LocalWorkspace, SSHWorkspace, WorkspaceError

FAKE_SSH = """#!/usr/bin/env bash
# test double for ssh: drop options and the target, run the remote command locally
printf '%s\\n' "$*" >> "$FAKE_SSH_LOG"
while [ $# -gt 0 ]; do
  case "$1" in -p|-o|-i) shift 2 ;; *@*) shift; break ;; *) shift ;; esac
done
exec bash -c "$*"
"""


@pytest.fixture
def fake_ssh(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    ssh = bindir / "ssh"
    ssh.write_text(FAKE_SSH)
    ssh.chmod(ssh.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "ssh.log"
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_SSH_LOG", str(log))
    return log


def test_ssh_workspace_streams_fixture_runs_and_removes(tmp_path, fake_ssh):
    base = tmp_path / "sandbox" / "bench"
    ws = SSHWorkspace(SSHTarget(host="sandbox", user="agent", port=2222, key="/keys/id", known_hosts="/tmp/kh"),
                      str(base))
    workdir = ws.stage(FIXTURES_DIR / "debug-py-paginacao", "bench-debug-py-paginacao-1-1")
    assert sorted(os.listdir(workdir)) == ["Makefile", "paginacao.py", "test_paginacao.py"]
    res = ws.run(workdir, "python3 -c 'import paginacao; print(\"it works\")' && exit 4", 30)
    assert res.exit_code == 4 and "it works" in res.output_tail and not res.timed_out
    slow = ws.run(workdir, "sleep 5", 1)
    assert slow.timed_out and slow.exit_code == 124
    ws.remove(workdir)
    assert not os.path.exists(workdir)
    calls = fake_ssh.read_text()
    assert "-p 2222" in calls and "-i /keys/id" in calls and "UserKnownHostsFile=/tmp/kh" in calls
    assert "BatchMode=yes" in calls and "agent@sandbox" in calls and "timeout -k 10" in calls


def test_ssh_probe_sends_env_like_hermes_terminal(fake_ssh, monkeypatch):
    monkeypatch.delenv("EDGE_API_KEY", raising=False)
    ws = SSHWorkspace(SSHTarget(host="sandbox"), "/workspace/bench")
    res = ws.probe('echo "key=$EDGE_API_KEY"; exit 3', {"EDGE_API_KEY": "bench-probe"}, 10)
    assert res.exit_code == 3 and "key=bench-probe" in res.output_tail
    assert "-o SendEnv=EDGE_API_KEY" in fake_ssh.read_text()


def test_command_output_never_carries_nul(tmp_path):
    ws = LocalWorkspace(tmp_path)
    res = ws.run(ws.stage(None, "nul"), "printf 'a\\000b'", 10)
    assert res.output_tail == "ab"


def test_rows_are_stripped_of_nul_before_postgres():
    detail = {"output_tail": "a\x00b", "nested": [{"k\x00": "\x00"}], "literal": "\\u0000 stays", "n": 1}
    assert strip_nul(detail) == {"output_tail": "ab", "nested": [{"k": ""}], "literal": "\\u0000 stays", "n": 1}
    assert strip_nul("erro\x00") == "erro" and strip_nul(None) is None


def test_ssh_workspace_guards_paths(tmp_path):
    with pytest.raises(WorkspaceError):
        SSHWorkspace(SSHTarget(host="s"), "relative/path")
    with pytest.raises(WorkspaceError):
        SSHWorkspace(SSHTarget(host="s"), "/")
    ws = SSHWorkspace(SSHTarget(host="s"), "/workspace/bench")
    with pytest.raises(WorkspaceError):
        ws.run("/etc", "true", 5)
    with pytest.raises(WorkspaceError):
        ws.remove("/workspace/other")
    with pytest.raises(WorkspaceError):
        ws.stage(None, "../escape")


def test_ssh_failure_is_an_infrastructure_error(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "ssh").write_text("#!/bin/sh\necho 'ssh: connect to host sandbox port 22: Connection refused' >&2\nexit 255\n")
    (bindir / "ssh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    ws = SSHWorkspace(SSHTarget(host="sandbox"), "/workspace/bench")
    with pytest.raises(WorkspaceError, match="Connection refused"):
        ws.run("/workspace/bench/x", "true", 5)
    with pytest.raises(WorkspaceError, match="staging"):
        ws.stage(None, "x")


def test_settings_from_env_defaults_and_profile_keys():
    s = Settings.from_env({"AIOS_DB_PASSWORD": "p@ss/word", "PG_HOST_PORT": "55432", "HERMES_API_KEY": "hk",
                           "HERMES_API_KEY_ENGINEERING": "ek", "HERMES_API_KEY_FINANCE": "",
                           "TERMINAL_SSH_HOST": "sandbox", "TERMINAL_SSH_PORT": "2222"})
    assert s.database_url == "postgresql://aios:p%40ss%2Fword@127.0.0.1:55432/aios"
    assert s.profile_keys == {"engineering": "ek"}
    assert s.ssh == SSHTarget(host="sandbox", user="agent", port=2222, key=None, known_hosts=None)
    assert s.hermes_url == "http://127.0.0.1:8642" and s.decision_url == "http://127.0.0.1:8090"
    assert s.judge_model == "tier5-sonnet" and s.task_timeout == 900
    assert s.tasks_dir.name == "tasks" and s.routing_file.name == "routing.yaml"
    explicit = Settings.from_env({"BENCH_DATABASE_URL": "postgresql://x/y", "AIOS_HERMES_URL": "http://hermes:8642/"})
    assert explicit.database_url == "postgresql://x/y" and explicit.hermes_url == "http://hermes:8642"
    assert Settings.from_env({}).database_url is None and Settings.from_env({}).ssh is None


def test_dotenv_never_overrides_real_env(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("# c\nexport A=1\nB='dois # não é comentário'\nC=3 # comentário\nD=\n")
    assert parse_dotenv(env_file.read_text()) == {"A": "1", "B": "dois # não é comentário", "C": "3", "D": ""}
    environ = {"BENCH_ENV_FILE": str(env_file), "A": "real"}
    assert load_dotenv(environ) == env_file
    assert environ["A"] == "real" and environ["C"] == "3"
    assert load_dotenv({"BENCH_ENV_FILE": str(tmp_path / "missing")}) is None


def test_json_logs_carry_extra_fields():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logs.JsonFormatter())
    log = logging.getLogger("bench.test")
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    try:
        log.info("task done", extra={"task": "simple-x", "success": True})
    finally:
        log.removeHandler(handler)
    line = json.loads(stream.getvalue())
    assert line["msg"] == "task done" and line["task"] == "simple-x" and line["success"] is True
    assert line["level"] == "info" and line["logger"] == "bench.test" and "ts" in line
