"""coding/repository_analysis/scripts/repo_map.sh and knowledge/transcript_to_notes/scripts/edge_pipeline.sh."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from skilltest import run, run_json, script

REPO_MAP = script("coding/repository_analysis/scripts/repo_map.sh")
EDGE = script("knowledge/transcript_to_notes/scripts/edge_pipeline.sh")
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(not BASH, reason="bash not available")


# ----------------------------------------------------------------------------- repo_map.sh

def _git(repo: Path, *args: str) -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": "Ana Dev", "GIT_AUTHOR_EMAIL": "ana@example.com",
           "GIT_COMMITTER_NAME": "Ana Dev", "GIT_COMMITTER_EMAIL": "ana@example.com", "GIT_CONFIG_GLOBAL": os.devnull}
    proc = run(["git", "-C", str(repo), *args], env=env)
    assert proc.returncode == 0, proc.stderr


@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    if not shutil.which("git"):
        pytest.skip("git not available")
    repo = tmp_path / "repo"
    files = {
        "go.mod": "module example.com/app\n\ngo 1.23\n",
        "main.go": "package main\n\n// TODO: flags\nfunc main() {}\n",
        "cmd/worker/main.go": "package main\n\nfunc main() {}\n",
        "internal/store/store.go": "package store\n\n// FIXME: retry\n// XXX and HACK too\n",
        "package.json": '{\n  "name": "web",\n  "main": "dist/index.js",\n  "scripts": {"test": "vitest run", '
                        '"build": "tsc -p .", "lint": "eslint ."}\n}\n',
        "pnpm-lock.yaml": "lockfileVersion: '9.0'\n",
        "tsconfig.json": "{}\n",
        "src/index.ts": "export const x = 1\n",
        "Makefile": "VAR := 1\ntest:\n\tgo test ./...\nlint:\n\tgo vet ./...\nbuild:\n\tgo build ./...\n",
        "docs/relatório.md": "# relatório\n",
        'notes/a "quoted" name.txt': "x\n",
        ".github/workflows/ci.yml": "jobs:\n  t:\n    steps:\n      - run: go test -race ./...\n",
    }
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    (repo / ".gitignore").write_text("dist/\n", encoding="utf-8")
    (repo / "dist").mkdir()
    (repo / "dist" / "index.js").write_text("// TODO ignored\n", encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", 'feat: initial "quoted"\tcommit')
    (repo / "untracked.py").write_text("print(1)  # TODO later\n", encoding="utf-8")
    return repo


def test_repo_map_json_on_git_repo(sample_repo: Path):
    out = run_json([BASH, str(REPO_MAP), str(sample_repo)])
    git = out["git"]
    assert git["is_repo"] is True and git["branch"] == "main" and len(git["head"]) >= 7
    assert git["dirty_files"] == 1 and git["commits_30d"] == 1
    assert git["recent_commits"][0]["subject"] == 'feat: initial "quoted"\tcommit'
    assert git["recent_commits"][0]["author"] == "Ana Dev"
    assert out["files_total"] == 14  # tracked + untracked, .gitignore'd dist/ excluded
    langs = {entry["language"]: entry["files"] for entry in out["languages"]}
    assert langs["Go"] == 3 and langs["TypeScript"] == 1 and langs["Python"] == 1
    assert {"go.mod", "package.json", "Makefile", "tsconfig.json", ".github/workflows/ci.yml"} <= set(out["manifests"])
    cmds = out["commands"]
    assert {"go build ./...", "pnpm run build", "make build"} <= set(cmds["build"])
    assert {"go test ./...", "pnpm run test", "make test"} <= set(cmds["test"])
    assert {"go vet ./...", "pnpm run lint", "pnpm exec tsc --noEmit", "make lint"} <= set(cmds["lint"])
    assert "make VAR" not in " ".join(sum(cmds.values(), []))
    assert cmds["ci"] == ["go test -race ./..."]
    expected_entrypoints = {"main.go", "cmd/worker/main.go", "src/index.ts", "package.json main: dist/index.js"}
    assert expected_entrypoints <= set(out["entrypoints"])
    tree = {entry["path"]: entry["files"] for entry in out["tree"]}
    assert tree["internal"] == 1 and tree["internal/store"] == 1 and "dist" not in tree
    assert "docs/relatório.md" not in out["root_files"] and "go.mod" in out["root_files"]
    assert out["todos"]["total"] == 4  # lines carrying a marker; dist/ is ignored, untracked files count
    assert out["todos"]["top_files"][0] == {"count": 2, "path": "internal/store/store.go"}


def test_repo_map_markdown_and_non_git(tmp_path: Path):
    plain = tmp_path / "plain"
    (plain / "node_modules" / "dep").mkdir(parents=True)
    (plain / "node_modules" / "dep" / "index.js").write_text("// TODO no\n", encoding="utf-8")
    (plain / "app").mkdir()
    (plain / "app" / "main.py").write_text("# FIXME yes\n", encoding="utf-8")
    (plain / "pyproject.toml").write_text("[project]\nname='x'\n[tool.ruff]\nline-length=100\n", encoding="utf-8")
    (plain / "tests").mkdir()
    out = run_json([BASH, str(REPO_MAP), str(plain)])
    assert out["git"]["is_repo"] is False and out["files_total"] == 2
    assert out["commands"]["test"] == ["uv run --with pytest pytest -q"] and out["commands"]["lint"] == ["ruff check ."]
    assert out["todos"]["total"] == 1
    md = run([BASH, str(REPO_MAP), "--format", "md", str(plain)])
    assert md.returncode == 0 and md.stdout.startswith("# Mapa do repositório:")
    assert "## Comandos" in md.stdout and "`uv run --with pytest pytest -q`" in md.stdout


@pytest.mark.parametrize("files, expected", [
    ({"pyproject.toml": "[project]\nname='x'\ndependencies=['pytest']\n[tool.mypy]\n[build-system]\n"
                        "requires=['hatchling']\n", "tests/test_a.py": ""},
     {"test": ["uv run pytest -q"], "lint": ["uv run --with mypy mypy ."], "build": ["uv build"]}),
    ({"requirements.txt": "requests\n", "setup.py": "", "mypy.ini": "", "tests/test_a.py": ""},
     {"test": ["uv run --no-project --with-requirements requirements.txt --with pytest pytest -q"],
      "lint": ["uv run --no-project --with-requirements requirements.txt --with mypy mypy ."],
      "build": ["uv build"]}),
    ({"pyproject.toml": "[tool.poetry]\nname='x'\n[build-system]\nrequires=['poetry-core']\n",
      "poetry.lock": "", "conftest.py": ""},
     {"test": ["poetry run pytest -q"], "lint": [], "build": ["poetry build"]}),
], ids=["pep621", "requirements", "poetry"])
def test_repo_map_python_commands_run_in_the_sandbox(tmp_path: Path, files: dict, expected: dict):
    """The sandbox has python3 + uv + ruff only: no bare `python`, no global pytest/mypy."""
    for rel, content in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(content, encoding="utf-8")
    cmds = run_json([BASH, str(REPO_MAP), str(tmp_path)])["commands"]
    assert {k: cmds[k] for k in expected} == expected
    assert not any(c.startswith("python ") or " python -m" in c for c in sum(cmds.values(), []))


def test_repo_map_output_stays_valid_utf8_with_non_utf8_paths(tmp_path: Path):
    if not shutil.which("git"):
        pytest.skip("git not available")
    repo = tmp_path / "repo"
    repo.mkdir()
    try:
        (repo / os.fsdecode(b"bad\xffname.txt")).write_text("x\n", encoding="utf-8")
    except (OSError, UnicodeError):
        pytest.skip("filesystem rejects non-UTF-8 names")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    proc = subprocess.run([BASH, str(REPO_MAP), str(repo)], capture_output=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.decode("utf-8"))
    assert out["root_files"] == ["badname.txt"]
    md = subprocess.run([BASH, str(REPO_MAP), "--format", "md", str(repo)], capture_output=True, timeout=60)
    assert md.returncode == 0 and "badname.txt" in md.stdout.decode("utf-8")


def test_repo_map_usage_errors(tmp_path: Path):
    assert run([BASH, str(REPO_MAP), str(tmp_path / "missing")]).returncode == 2
    assert run([BASH, str(REPO_MAP), "--format", "xml", str(tmp_path)]).returncode == 2
    assert run([BASH, str(REPO_MAP), "--commits", "x", str(tmp_path)]).returncode == 2


# ----------------------------------------------------------------------------- edge_pipeline.sh

class FakeEdge(BaseHTTPRequestHandler):
    """Implements the slice of the edge API the script uses (workers/edge/src/edge/app.py)."""

    key = "test-key"
    requests: list[dict] = []
    polls: dict[str, int] = {}

    def log_message(self, *_):  # keep pytest output clean
        pass

    def _reply(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        if self.headers.get("Authorization") != f"Bearer {self.key}":
            self._reply(401, {"error": "unauthorized"})
            return False
        return True

    def do_POST(self):
        if not self._authorized():
            return
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path == "/upload":
            assert b'name="file"; filename="aula.m4a"' in raw
            self.requests.append({"path": "/upload"})
            return self._reply(201, {"id": "u1", "uri": "storage://media/input/u1.m4a", "filename": "aula.m4a"})
        body = json.loads(raw)
        self.requests.append({"path": self.path, "body": body})
        if body["source"].endswith("fail.m4a"):
            return self._reply(400, {"error": "fonte inválida"})
        if body.get("async"):
            return self._reply(202, {"job_id": "job1", "status": "queued", "status_url": "/jobs/job1"})
        return self._reply(200, {"id": "m1", "tenant": body["tenant"], "txt_uri": "storage://media/t/m1.txt"})

    def do_GET(self):
        if not self._authorized():
            return
        job = self.path.rsplit("/", 1)[-1]
        self.polls[job] = self.polls.get(job, 0) + 1
        if job == "stuck" or self.polls[job] < 2:
            return self._reply(200, {"job_id": job, "status": "running"})
        if job == "broken":
            return self._reply(200, {"job_id": job, "status": "failed", "error": {"error": "whisper down"}})
        return self._reply(200, {"job_id": job, "status": "succeeded",
                                 "result": {"id": "m1", "txt_uri": "storage://media/t/m1.txt", "ingest": [{"ok": 1}]}})


@pytest.fixture
def edge_env():
    FakeEdge.requests, FakeEdge.polls = [], {}
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeEdge)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = {**{k: v for k, v in os.environ.items() if k != "HERMES_TENANT"},
           "AIOS_EDGE_URL": f"http://127.0.0.1:{server.server_port}", "EDGE_API_KEY": FakeEdge.key,
           "AIOS_EDGE_TIMEOUT": "20"}
    yield env
    server.shutdown()
    server.server_close()


needs_curl = pytest.mark.skipif(not shutil.which("curl"), reason="curl not available")


@needs_curl
def test_edge_pipeline_async_job_is_polled_to_completion(edge_env):
    out = run_json([BASH, str(EDGE), "--domain", "learning", "--title", 'Aula "05"', "--poll", "1",
                    "storage://media/input/aula05.m4a", "pessoal"], env=edge_env)
    assert out == {"id": "m1", "txt_uri": "storage://media/t/m1.txt", "ingest": [{"ok": 1}]}
    sent = FakeEdge.requests[0]
    assert sent["path"] == "/pipeline"
    assert sent["body"] == {"source": "storage://media/input/aula05.m4a", "tenant": "pessoal", "ingest": True,
                            "domain": "learning", "title": 'Aula "05"', "async": True}
    assert FakeEdge.polls["job1"] == 2


@needs_curl
def test_edge_pipeline_sync_and_upload(edge_env, tmp_path: Path):
    out = run_json([BASH, str(EDGE), "--sync", "--no-ingest", "storage://media/input/a.wav", "nitro"], env=edge_env)
    assert out["tenant"] == "nitro" and FakeEdge.requests[-1]["body"]["ingest"] is False
    media = tmp_path / "aula.m4a"
    media.write_bytes(b"\x00\x01fake")
    proc = run([BASH, str(EDGE), "--upload", str(media), "--poll", "1", "pessoal"], env=edge_env)
    assert proc.returncode == 0, proc.stderr
    assert "storage://media/input/u1.m4a" in proc.stderr
    assert FakeEdge.requests[-1]["body"]["source"] == "storage://media/input/u1.m4a"


@needs_curl
def test_edge_pipeline_error_exit_codes(edge_env):
    http_err = run([BASH, str(EDGE), "storage://media/input/fail.m4a", "pessoal"], env=edge_env)
    assert http_err.returncode == 4 and "HTTP 400" in http_err.stderr
    failed = run([BASH, str(EDGE), "--job", "broken", "--poll", "1"], env=edge_env)
    assert failed.returncode == 4 and "failed" in failed.stderr
    stuck = run([BASH, str(EDGE), "--job", "stuck", "--poll", "1"], env={**edge_env, "AIOS_EDGE_TIMEOUT": "1"})
    assert stuck.returncode == 6 and "--job stuck" in stuck.stderr
    unauthorized = run([BASH, str(EDGE), "storage://media/input/a.wav", "pessoal"],
                       env={**edge_env, "EDGE_API_KEY": "x"})
    assert unauthorized.returncode == 4 and "401" in unauthorized.stderr
    down = run([BASH, str(EDGE), "storage://media/input/a.wav", "pessoal"],
               env={**edge_env, "AIOS_EDGE_URL": "http://127.0.0.1:9"})
    assert down.returncode == 5


@needs_curl
def test_edge_pipeline_enforces_the_session_tenant(edge_env, tmp_path: Path):
    """The aios guard never sees the tenant inside a shell string: the script checks HERMES_TENANT."""
    worker = {**edge_env, "HERMES_TENANT": "pessoal"}
    other = run([BASH, str(EDGE), "--sync", "storage://media/input/a.wav", "nitro"], env=worker)
    assert other.returncode == 7 and "pessoal" in other.stderr and FakeEdge.requests == []
    media = tmp_path / "aula.m4a"
    media.write_bytes(b"x")
    upload = run([BASH, str(EDGE), "--upload", str(media), "shared"], env=worker)
    assert upload.returncode == 7 and FakeEdge.requests == []
    same = run_json([BASH, str(EDGE), "--sync", "storage://media/input/a.wav", "pessoal"], env=worker)
    assert same["tenant"] == "pessoal"


def test_edge_pipeline_validates_inputs_before_calling(tmp_path: Path):
    env = {**os.environ, "EDGE_API_KEY": "k", "AIOS_EDGE_URL": "http://127.0.0.1:9"}
    assert run([BASH, str(EDGE), "/home/x/aula.m4a", "pessoal"], env=env).returncode == 2
    assert run([BASH, str(EDGE), "storage://media/input/a.wav", "Pessoal!"], env=env).returncode == 2
    assert run([BASH, str(EDGE), "storage://media/input/a.wav"], env=env).returncode == 2
    assert run([BASH, str(EDGE), "--upload", str(tmp_path / "missing.wav"), "pessoal"], env=env).returncode == 2
    no_key = {k: v for k, v in env.items() if k != "EDGE_API_KEY"}
    proc = run([BASH, str(EDGE), "storage://media/input/a.wav", "pessoal"], env=no_key)
    assert proc.returncode == 3 and "EDGE_API_KEY" in proc.stderr
