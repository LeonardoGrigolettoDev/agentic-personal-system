#!/usr/bin/env bash
# shellcheck disable=SC2016  # single-quoted `bash -c` snippets receive their values as positional args
# Sandbox tests without Docker: every aios-* helper against throwaway git repos, the aios plugin's
# run_check contract (through a fake ssh), static checks of the image files and the make targets.
#   bash workers/sandbox/tests/run.sh
# Optional: SANDBOX_SSHD_BIN=/path/to/sshd (config test + end-to-end SSH run as the current user; add
# SANDBOX_SSHD_LIBS=<dir> as its LD_LIBRARY_PATH if needed), JQ_BIN=/path/to/jq (entrypoint log filter).
# Exit status 0 only when no test failed (skips are reported, never hidden).
set -uo pipefail

HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
SANDBOX=$(dirname -- "$HERE")
REPO=$(cd -- "$SANDBOX/../.." && pwd -P)
BIN=$SANDBOX/bin
export PATH="$BIN:$HOME/.local/bin:$PATH"

TMP=$(mktemp -d "${TMPDIR:-/tmp}/aios-sandbox-test.XXXXXX")
cleanup() {
  [[ -n ${LISTENER_PID:-} ]] && kill "$LISTENER_PID" 2>/dev/null
  [[ -n ${SSHD_PID:-} ]] && kill "$SSHD_PID" 2>/dev/null
  chmod -R u+rwX -- "$TMP" 2>/dev/null
  rm -rf -- "$TMP"
}
trap cleanup EXIT

export WORKSPACE_ROOT=$TMP/ws
export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=$TMP/gitconfig GIT_TERMINAL_PROMPT=0
export AIOS_CHECK_TIMEOUT=300
unset GITHUB_TOKEN AIOS_GITHUB_TOKEN_FILE AIOS_PYTHON AIOS_TASK_CHECK_TIMEOUT
unset MAKEFLAGS MAKELEVEL MFLAGS # `make sandbox-test` must not turn fixture output into make[1]: lines
mkdir -p "$WORKSPACE_ROOT" "$TMP/remotes" "$TMP/fixtures"
cat >"$GIT_CONFIG_GLOBAL" <<EOF
[user]
	name = Sandbox Test
	email = test@aios.invalid
[init]
	defaultBranch = main
[advice]
	detachedHead = false
[url "file://$TMP/remotes/"]
	insteadOf = https://git.example.test/
EOF

PASSED=0 FAILED=0 SKIPPED=0
OUT="" ERR="" RC=0
section() { printf '\n== %s\n' "$*"; }
pass() {
  PASSED=$((PASSED + 1))
  printf '  ok    %s\n' "$1"
}
fail() {
  FAILED=$((FAILED + 1))
  printf '  FAIL  %s\n' "$1"
  if [[ -n ${2:-} ]]; then printf '%s\n' "$2" | tail -n 40 | sed 's/^/        | /'; fi
}
skip() {
  SKIPPED=$((SKIPPED + 1))
  printf '  skip  %s (%s)\n' "$1" "$2"
}
# run CMD...: OUT=stdout, ERR=stderr, RC=exit status
run() {
  OUT=$("$@" 2>"$TMP/.stderr")
  RC=$?
  ERR=$(<"$TMP/.stderr")
}
details() { printf 'rc=%s\nstdout:\n%s\nstderr:\n%s' "$RC" "$OUT" "$ERR"; }
check() { # check NAME CONDITION-COMMAND...
  local name=$1
  shift
  if "$@"; then pass "$name"; else fail "$name" "$(details)"; fi
}
# jcheck NAME PY-EXPR: evaluates PY-EXPR with d = json.loads(last stdout line); also requires that stdout
# is exactly one line (the --json contract).
jcheck() {
  local name=$1 expr=$2
  if [[ $(printf '%s\n' "$OUT" | wc -l) == 1 ]] && python3 -I -c '
import json, sys
d = json.loads(sys.argv[1].strip().splitlines()[-1])
sys.exit(0 if eval(sys.argv[2], {}, {"d": d}) else 1)' "$OUT" "$expr" 2>/dev/null; then
    pass "$name"
  else
    fail "$name" "expected: $expr"$'\n'"$(details)"
  fi
}

commit_all() { git -C "$1" add -A && git -C "$1" commit -qm "${2:-init}"; }
new_repo() {
  rm -rf -- "$1"
  mkdir -p "$1"
  git -C "$1" init -q
}
fixture_go() { # DIR pass|fail
  new_repo "$1"
  mkdir -p "$1/calc"
  printf 'module example.test/calc\n\ngo 1.22\n' >"$1/go.mod"
  printf 'package calc\n\nfunc Add(a, b int) int { return a + b }\n' >"$1/calc/add.go"
  local want=3
  [[ $2 == fail ]] && want=4
  printf 'package calc\n\nimport "testing"\n\nfunc TestAdd(t *testing.T) {\n\tif got := Add(1, 2); got != %s {\n\t\tt.Fatalf("Add(1, 2) = %%d", got)\n\t}\n}\n' "$want" >"$1/calc/add_test.go"
  printf 'old\n' >"$1/remove-me.txt"
  commit_all "$1"
}
fixture_py() { # DIR pass|fail dev|none
  new_repo "$1"
  mkdir -p "$1/src/calc" "$1/tests"
  {
    printf '[project]\nname = "calc"\nversion = "0.1.0"\nrequires-python = ">=3.10"\ndependencies = []\n'
    [[ $3 == dev ]] && printf '\n[dependency-groups]\ndev = ["pytest"]\n'
    printf '\n[tool.pytest.ini_options]\npythonpath = ["src"]\n'
  } >"$1/pyproject.toml"
  printf 'def add(a, b):\n    return a + b\n' >"$1/src/calc/__init__.py"
  local want=3
  [[ $2 == fail ]] && want=4
  printf 'from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == %s\n' "$want" >"$1/tests/test_calc.py"
  printf '.venv/\n__pycache__/\n' >"$1/.gitignore"
  commit_all "$1"
}

GO_OK=0
command -v go >/dev/null 2>&1 && GO_OK=1
NODE_OK=0
command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1 && NODE_OK=1

# =========================================================================== static checks
section "static checks"
shell_files=("$BIN"/* "$SANDBOX"/lib/aios/*.sh "$SANDBOX/entrypoint.sh" "$HERE"/*.sh)
syntax_ok=1
for f in "${shell_files[@]}"; do
  bash -n "$f" 2>"$TMP/.stderr" || {
    syntax_ok=0
    fail "bash -n $(basename "$f")" "$(<"$TMP/.stderr")"
  }
done
((syntax_ok)) && pass "bash -n on ${#shell_files[@]} scripts"
for f in profile.sh agent-profile.sh; do
  if sh -n "$SANDBOX/$f" 2>"$TMP/.stderr"; then pass "sh -n $f"; else fail "sh -n $f" "$(<"$TMP/.stderr")"; fi
done
if python3 -I -c 'import ast, sys; ast.parse(open(sys.argv[1]).read())' "$SANDBOX/lib/aios/aios_helper.py"; then
  pass "aios_helper.py parses"
else
  fail "aios_helper.py parses"
fi
for f in "$BIN"/*; do
  [[ -x $f ]] || fail "$(basename "$f") is executable"
done

SHELLCHECK=()
if command -v shellcheck >/dev/null 2>&1; then
  SHELLCHECK=(shellcheck)
elif command -v uvx >/dev/null 2>&1 && timeout 120 uvx --quiet --from shellcheck-py shellcheck --version >/dev/null 2>&1; then
  SHELLCHECK=(uvx --quiet --from shellcheck-py shellcheck)
fi
if ((${#SHELLCHECK[@]})); then
  run "${SHELLCHECK[@]}" -x -P SCRIPTDIR -s bash "${shell_files[@]}"
  check "shellcheck (bash)" test "$RC" -eq 0
  run "${SHELLCHECK[@]}" -s sh "$SANDBOX/profile.sh" "$SANDBOX/agent-profile.sh"
  check "shellcheck (profile.sh, agent-profile.sh, sh)" test "$RC" -eq 0
else
  skip "shellcheck" "neither shellcheck nor uvx --from shellcheck-py available"
fi

# Dockerfile pins: every checksum is 64 hex chars, downloads are verified before use, tini is PID 1.
dockerfile=$SANDBOX/Dockerfile
pins_ok=1
for a in GO_SHA256 NODE_SHA256 UV_SHA256 DUCKDB_SHA256; do
  grep -Eq "^ARG $a=[0-9a-f]{64}$" "$dockerfile" || pins_ok=0
done
[[ $(grep -c 'sha256sum -c -' "$dockerfile") -eq 4 ]] || pins_ok=0
grep -Eq '^ARG DEBIAN_IMAGE=debian:trixie-slim@sha256:[0-9a-f]{64}$' "$dockerfile" || pins_ok=0
grep -q '^ENTRYPOINT \["/usr/bin/tini", "--"' "$dockerfile" || pins_ok=0
if ((pins_ok)); then pass "Dockerfile pins base image + 4 checksummed downloads"; else fail "Dockerfile pins"; fi

# PATH: system directories before the agent-writable ones everywhere a session gets its PATH from.
run env -i HOME=/h PATH=/usr/local/bin:/usr/bin:/bin sh -c '. "$1"; printf %s "$PATH"' _ "$SANDBOX/profile.sh"
check "profile.sh appends the user tool dirs after the system ones" \
  test "$OUT" = /usr/local/bin:/usr/bin:/bin:/usr/local/go/bin:/h/go/bin:/h/.local/bin
check "entrypoint SetEnv PATH lists system dirs first" \
  grep -qF 'PATH=/usr/local/bin:/usr/bin:/bin:/usr/local/go/bin:$home/go/bin:$home/.local/bin"' "$SANDBOX/entrypoint.sh"
check "agent ~/.profile (replaces Debian skel, which prepends ~/.local/bin) sets no PATH" \
  bash -c '! grep -Eq "^[^#]*PATH=" "$1" && grep -q "agent-profile.sh /home/agent/.profile" "$2"' _ "$SANDBOX/agent-profile.sh" "$dockerfile"

if python3 -I -c 'import yaml' 2>/dev/null; then
  run python3 -I - "$REPO/compose.yaml" <<'PY'
import sys, yaml
doc = yaml.safe_load(open(sys.argv[1]))
s = doc["services"]["sandbox"]
errors = []
on_net = sorted(n for n, svc in doc["services"].items() if "sandbox_net" in (svc.get("networks") or []))
if on_net != ["bench", "edge", "hermes", "sandbox"]:
    errors.append(f"only the sandbox and its clients (hermes, edge, bench) share sandbox_net, got {on_net}")
want = lambda cond, msg: cond or errors.append(msg)
want(s.get("profiles") == ["agent"], "profile agent")
want(s.get("networks") == ["sandbox_net"], "networks [sandbox_net] only (not aios: ufw admits it to Ollama)")
net = doc.get("networks", {}).get("sandbox_net") or {}
subnets = [c.get("subnet") for c in (net.get("ipam") or {}).get("config", [])]
want(subnets == ["172.30.1.0/24"], "sandbox_net subnet 172.30.1.0/24 (outside ufw's 172.30.0.0/24)")
want(not net.get("internal"), "sandbox_net keeps egress (not internal)")
env = s.get("environment", {})
want(env.get("AIOS_CHECK_TIMEOUT", "").endswith(":-570}") and env.get("AIOS_TASK_CHECK_TIMEOUT", "").endswith(":-280}"),
     "check budgets 570 (plugin) / 280 (aios-task-check)")
want("ports" not in s, "no published ports")
want(s.get("cap_drop") == ["ALL"], "cap_drop ALL")
want(set(s.get("cap_add", [])) <= {"CHOWN", "SETUID", "SETGID", "SYS_CHROOT", "AUDIT_WRITE", "KILL"}, "minimal cap_add")
want("no-new-privileges:true" in s.get("security_opt", []), "no-new-privileges")
want(s.get("mem_limit") == "2g" and s.get("memswap_limit") == "2g", "2g memory, no swap")
want(s.get("pids_limit") == 512 and float(s.get("cpus")) == 2.0, "pids 512, 2 cpus")
vols = [v if isinstance(v, str) else f"{v['source']}:{v['target']}" for v in s.get("volumes", [])]
want(not any("docker.sock" in v for v in vols), "no docker.sock")
want(any(v.startswith("./data/sandbox/workspace:/workspace") for v in vols), "workspace bind")
want(any(v.startswith("sandbox_hostkeys:/etc/ssh/keys") for v in vols), "host key volume")
want("sandbox_hostkeys" in doc.get("volumes", {}), "named volume declared")
want(s.get("healthcheck", {}).get("test") == ["CMD", "/usr/local/bin/aios-healthcheck"], "healthcheck")
print("\n".join(errors))
sys.exit(1 if errors else 0)
PY
  check "compose.yaml sandbox: profile, network (+ clients), limits, caps, volumes, healthcheck, budgets" test "$RC" -eq 0
else
  skip "compose.yaml sandbox structure" "PyYAML not importable"
fi

SSHD=${SANDBOX_SSHD_BIN:-$(command -v sshd || true)}
if [[ -n $SSHD && -x $SSHD ]]; then
  ssh-keygen -q -t ed25519 -N '' -f "$TMP/hostkey"
  sshd_extra=()
  session=$(dirname "$SSHD")/../lib/openssh
  [[ -x $session/sshd-session ]] && sshd_extra+=(-o "SshdSessionPath=$session/sshd-session" -o "SshdAuthPath=$session/sshd-auth")
  run "$SSHD" -t -f "$SANDBOX/sshd_config" -o "HostKey=$TMP/hostkey" -o PidFile=none "${sshd_extra[@]}"
  check "sshd -t accepts sshd_config" test "$RC" -eq 0
else
  skip "sshd -t" "no sshd binary (set SANDBOX_SSHD_BIN)"
fi

JQ=${JQ_BIN:-$(command -v jq || true)}
if [[ -n $JQ && -x $JQ ]]; then
  filter=$(python3 -I -c '
import re, sys
m = re.search(r"jq --unbuffered -Rc (\x27)(.*?)\1 >&2\)", open(sys.argv[1]).read(), re.S)
print(m.group(2))' "$SANDBOX/entrypoint.sh")
  run bash -c 'printf "Server listening on 0.0.0.0 port 22.\r\nerror: kex_exchange_identification: x\r\nfatal: boom\r\n" | "$1" --unbuffered -Rc "$2"' _ "$JQ" "$filter"
  check "entrypoint wraps sshd lines as JSON with levels" python3 -I -c '
import json, sys
rows = [json.loads(l) for l in sys.argv[1].splitlines()]
assert [r["level"] for r in rows] == ["info", "error", "fatal"], rows
assert rows[0]["msg"] == "Server listening on 0.0.0.0 port 22." and rows[0]["component"] == "sshd"' "$OUT"
else
  skip "entrypoint jq log filter" "no jq (set JQ_BIN)"
fi

# =========================================================================== aios_helper.py
section "aios_helper.py"
helper() { python3 -I "$SANDBOX/lib/aios/aios_helper.py" "$@"; }
mkdir -p "$TMP/helper"
pyinfo() { printf '%s\n' "$1" >"$TMP/helper/pyproject.toml" && helper py-info "$TMP/helper/pyproject.toml"; }
run pyinfo $'[project]\nname="x"\ndependencies=["pytest>=8"]'
check "py-info: project dependency -> mode dev" test "$OUT" = $'table=project\nbuild=0\nmode=dev'
run pyinfo $'[project]\nname="x"\n[dependency-groups]\ndev=[{include-group="test"}]\ntest=["PyTest[testing]==9.1"]'
check "py-info: included group of dev -> mode dev" test "$OUT" = $'table=project\nbuild=0\nmode=dev'
run pyinfo $'[project]\nname="x"\n[dependency-groups]\nqa=["pytest"]\n[build-system]\nrequires=["hatchling"]'
check "py-info: non-default group -> group:qa, build-system seen" test "$OUT" = $'table=project\nbuild=1\nmode=group:qa'
run pyinfo $'[project]\nname="x"\n[project.optional-dependencies]\ntest=["pytest-cov", "pytest; python_version>\'3.9\'"]'
check "py-info: extra -> extra:test" test "$OUT" = $'table=project\nbuild=0\nmode=extra:test'
run pyinfo $'[project]\nname="x"\ndependencies=["pytest-cov"]\n[tool.pytest.ini_options]\naddopts="-q"'
check "py-info: only plugins/config -> mode none" test "$OUT" = $'table=project\nbuild=0\nmode=none'
run pyinfo $'[tool.ruff]\nline-length=100\n[tool.pytest.ini_options]\naddopts="-q"'
check "py-info: tool-only pyproject -> table none" test "$OUT" = $'table=none\nbuild=0'
run pyinfo $'[tool.poetry]\nname="p"\n[tool.poetry.dependencies]\npython="^3.10"\nsix="^1.16"\n[tool.poetry.group.dev.dependencies]\npytest-mock="^3.10"\n[tool.poetry.group.docs]\noptional=true\n[tool.poetry.group.docs.dependencies]\nmkdocs="*"\n[build-system]\nrequires=["poetry-core"]\nbuild-backend="poetry.core.masonry.api"'
check "py-info: installable Poetry -> editable install + non-optional group deps" \
  test "$OUT" = $'table=poetry\nbuild=1\ninstallable=1\nwith=pytest-mock>=3.10,<4'
run pyinfo $'[tool.poetry]\nname="p"\npackage-mode=false\n[tool.poetry.dependencies]\npython="^3.10"\nsix="^1.16"\n[tool.poetry.dev-dependencies]\nfoo={git="https://x.test/foo.git"}'
check "py-info: Poetry app -> main deps via --with, git dependency unsupported" \
  test "$OUT" = $'table=poetry\nbuild=0\ninstallable=0\nwith=six>=1.16,<2\nunsupported=foo'
run pyinfo $'[project\nname='
check "py-info: invalid TOML -> table invalid" test "$OUT" = $'table=invalid\nbuild=0'
run python3 -I - "$SANDBOX/lib/aios/aios_helper.py" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("aios_helper", sys.argv[1])
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)
cases = {"^1.2.3": ">=1.2.3,<2", "^0.2.3": ">=0.2.3,<0.3", "^0.0.3": ">=0.0.3,<0.0.4", "^0": ">=0,<1",
         "^0.0": ">=0.0,<0.1", "~1.2.3": ">=1.2.3,<1.3", "~1.2": ">=1.2,<1.3", "~1": ">=1,<2", "*": "",
         ">=1.2, <1.5": ">=1.2,<1.5", ">=1.2 <2": ">=1.2,<2", "~=1.4": "~=1.4", "1.2.3": "==1.2.3",
         "1.2.*": "==1.2.*", "!=1.5,>1": "!=1.5,>1", "^1.0 || ^2.0": "", "^1.0.0b1": ""}
wrong = {c: h.poetry_constraint(c) for c, want in cases.items() if h.poetry_constraint(c) != want}
assert not wrong, wrong
assert h.poetry_requirement("uvicorn", {"version": "^0.30", "extras": ["standard"]}) == "uvicorn[standard]>=0.30,<0.31"
assert h.poetry_requirement("lib", {"path": "../lib", "develop": True}) is None
assert h.poetry_requirement("x", [{"version": "^1", "python": "<3.9"}, {"version": "^2", "python": ">=3.9"}]) == "x"
print("ok")
PY
check "Poetry constraints -> PEP 440 (caret, tilde, wildcard, ranges; no PEP 440 form -> any version)" test "$OUT" = ok
printf '{"scripts":{"test":"vitest run"},"devDependencies":{"vitest":"^3"},"packageManager":"pnpm@10.1.0+sha512.abc"}' >"$TMP/helper/package.json"
run helper npm-info "$TMP/helper/package.json"
check "npm-info: test script, deps, pnpm" test "$OUT" = $'test=1\ndeps=1\npm=pnpm'
printf '{"scripts":{"test":"echo \\"Error: no test specified\\" && exit 1"}}' >"$TMP/helper/package.json"
run helper npm-info "$TMP/helper/package.json"
check "npm-info: npm init placeholder is not a test" test "$OUT" = $'test=0\ndeps=0\npm='
run bash -c 'printf "\033[31mred\033[0m line\nnon-utf8 \xff\n" | python3 -I "$1" result --exit-code=1 "--command=-x" --root=/r --duration-ms=5' _ "$SANDBOX/lib/aios/aios_helper.py"
jcheck "result: ANSI stripped, invalid UTF-8 replaced, ASCII JSON, dash-leading command" \
  'd["output_tail"] == "red line\nnon-utf8 \ufffd\n" and d["command"] == "-x" and d["exit_code"] == 1 and d["skipped"] is False and d["timed_out"] is False'

# =========================================================================== aios-check
section "aios-check"
if ((GO_OK)); then
  fixture_go "$WORKSPACE_ROOT/repos/go-pass" pass
  fixture_go "$WORKSPACE_ROOT/repos/go-fail" fail
  run aios-check --json "$WORKSPACE_ROOT/repos/go-pass/calc/add.go"
  jcheck "go module, passing tests -> exit 0" \
    'd["exit_code"] == 0 and d["command"] == "go test ./..." and not d["skipped"] and d["root"].endswith("/go-pass") and "ok" in d["output_tail"]'
  check "exit status mirrors the test command (0)" test "$RC" -eq 0
  run aios-check --json "$WORKSPACE_ROOT/repos/go-fail/calc"
  jcheck "go module, failing tests -> exit 1 with the failure in output_tail" \
    'd["exit_code"] == 1 and "Add(1, 2) = 3" in d["output_tail"] and "FAIL" in d["output_tail"] and not d["skipped"]'
  check "exit status mirrors the test command (1)" test "$RC" -eq 1
  run aios-check --json "$WORKSPACE_ROOT/repos/go-pass/calc/deleted_file.go"
  jcheck "a deleted file resolves to its existing parent" 'd["exit_code"] == 0 and d["command"] == "go test ./..."'
  run aios-check "$WORKSPACE_ROOT/repos/go-fail"
  check "human mode streams output and summarises on stderr" \
    bash -c '[[ $1 == 1 && $2 == *"--- FAIL"* && $3 == *"FAIL exit=1"* ]]' _ "$RC" "$OUT" "$ERR"

  # Makefile 'test' outranks go.mod in the same directory.
  fixture_go "$WORKSPACE_ROOT/repos/make-first" fail
  printf '.PHONY: test\ntest:\n\t@echo make-test-ran\n' >"$WORKSPACE_ROOT/repos/make-first/Makefile"
  run aios-check --json "$WORKSPACE_ROOT/repos/make-first/calc/add.go"
  jcheck "Makefile test target has priority over go.mod" 'd["command"] == "make test" and d["exit_code"] == 0 and "make-test-ran" in d["output_tail"]'

  # Monorepo: root has a Makefile without 'test' and a test/ dir; the nearest module wins.
  new_repo "$WORKSPACE_ROOT/repos/mono"
  mkdir -p "$WORKSPACE_ROOT/repos/mono/test"
  printf 'all:\n\t@true\n' >"$WORKSPACE_ROOT/repos/mono/Makefile"
  fixture_go "$TMP/fixtures/svc" pass
  cp -r "$TMP/fixtures/svc/." "$WORKSPACE_ROOT/repos/mono/services/svc/" 2>/dev/null || {
    mkdir -p "$WORKSPACE_ROOT/repos/mono/services/svc"
    cp -r "$TMP/fixtures/svc/." "$WORKSPACE_ROOT/repos/mono/services/svc/"
  }
  rm -rf "$WORKSPACE_ROOT/repos/mono/services/svc/.git"
  commit_all "$WORKSPACE_ROOT/repos/mono"
  run aios-check --json "$WORKSPACE_ROOT/repos/mono/services/svc/calc/add.go"
  jcheck "monorepo: nearest go.mod wins over a root Makefile without test" \
    'd["root"].endswith("/mono/services/svc") and d["command"] == "go test ./..." and d["exit_code"] == 0'

  # Not a git repo, but under WORKSPACE_ROOT.
  fixture_go "$WORKSPACE_ROOT/scratch/nogit" pass
  rm -rf "$WORKSPACE_ROOT/scratch/nogit/.git"
  run aios-check --json "$WORKSPACE_ROOT/scratch/nogit/calc/add.go"
  jcheck "project outside git under WORKSPACE_ROOT" 'd["exit_code"] == 0 and d["root"].endswith("/scratch/nogit")'
else
  skip "Go projects" "go not installed"
fi

new_repo "$WORKSPACE_ROOT/repos/no-tests"
printf '# docs only\n' >"$WORKSPACE_ROOT/repos/no-tests/README.md"
commit_all "$WORKSPACE_ROOT/repos/no-tests"
run aios-check --json "$WORKSPACE_ROOT/repos/no-tests/README.md"
jcheck "repo without tests -> skipped" \
  'd["skipped"] is True and d["exit_code"] == 0 and "no test command" in d["reason"] and d["root"].endswith("/no-tests")'
check "skipped exits 0" test "$RC" -eq 0

run aios-check --json
jcheck "usage error is still one JSON line (skipped)" 'd["skipped"] is True and "usage" in d["reason"]'
check "usage error exits 2" test "$RC" -eq 2
run aios-check --json --bogus x
check "unknown option exits 2" test "$RC" -eq 2
run env WORKSPACE_ROOT=/ aios-check --json "$WORKSPACE_ROOT/repos/no-tests"
jcheck "WORKSPACE_ROOT=/ -> configuration error as JSON" 'd["skipped"] is True and "configuration error" in d["reason"]'

new_repo "$WORKSPACE_ROOT/repos/slow"
printf 'test:\n\t@echo started; sleep 30\n' >"$WORKSPACE_ROOT/repos/slow/Makefile"
commit_all "$WORKSPACE_ROOT/repos/slow"
started=$SECONDS
run env AIOS_CHECK_TIMEOUT=2 aios-check --json "$WORKSPACE_ROOT/repos/slow"
jcheck "AIOS_CHECK_TIMEOUT kills the run -> exit 124, timed_out" \
  'd["exit_code"] == 124 and d["timed_out"] is True and "timed out after 2s" in d["output_tail"] and "started" in d["output_tail"]'
check "timeout honoured promptly" test $((SECONDS - started)) -lt 20

new_repo "$WORKSPACE_ROOT/repos/noisy"
printf 'test:\n\t@for i in $$(seq 1 500); do echo line-$$i; done; exit 7\n' >"$WORKSPACE_ROOT/repos/noisy/Makefile"
commit_all "$WORKSPACE_ROOT/repos/noisy"
run aios-check --json "$WORKSPACE_ROOT/repos/noisy"
jcheck "output_tail keeps the last 200 lines (make exits 2 on a failed recipe)" \
  'd["exit_code"] == 2 and len(d["output_tail"].splitlines()) == 200 and d["output_tail"].startswith("line-302\n") and "line-500\nmake: ***" in d["output_tail"]'

# The helper interpreter is pinned (AIOS_PYTHON, default /usr/bin/python3): a python3 the agent puts first in
# PATH (e.g. `uv python install --default` without tomllib) cannot break the JSON line; if the pinned one
# breaks, a minimal JSON line is printed anyway.
mkdir -p "$TMP/badpy"
printf '#!/bin/sh\necho "ModuleNotFoundError: No module named tomllib" >&2\nexit 1\n' >"$TMP/badpy/python3"
chmod +x "$TMP/badpy/python3"
new_repo "$WORKSPACE_ROOT/repos/maketest"
printf 'test:\n\t@echo make-ok\n' >"$WORKSPACE_ROOT/repos/maketest/Makefile"
commit_all "$WORKSPACE_ROOT/repos/maketest"
run env PATH="$TMP/badpy:$PATH" aios-check --json "$WORKSPACE_ROOT/repos/maketest"
jcheck "a broken python3 first in PATH does not reach the helper (pinned interpreter)" \
  'd["exit_code"] == 0 and "make-ok" in d["output_tail"] and "error" not in d'
run env AIOS_PYTHON="$TMP/badpy/python3" aios-check --json "$WORKSPACE_ROOT/repos/maketest"
jcheck "helper interpreter broken -> fallback JSON line with exit_code and command" \
  'd["exit_code"] == 0 and d["command"] == "make test" and d["skipped"] is False and d["root"].endswith("/maketest") and "error" in d'
check "fallback keeps the exit status of the command" test "$RC" -eq 0
run env AIOS_PYTHON="$TMP/badpy/python3" aios-check --json "$WORKSPACE_ROOT/repos/no-tests"
jcheck "helper interpreter broken -> skipped result still JSON" 'd["skipped"] is True and "no test command" in d["reason"]'

# Client gone (Hermes kills its ssh client on terminal.timeout; sshd only closes the session's pipes): the
# test command must stop at once and release the per-root lock, in both output modes.
new_repo "$WORKSPACE_ROOT/repos/orphan"
printf 'test:\n\t@if [ -f slow.flag ]; then echo started; sleep 4242; fi; echo quick\n' >"$WORKSPACE_ROOT/repos/orphan/Makefile"
printf 'slow.flag\n' >"$WORKSPACE_ROOT/repos/orphan/.gitignore"
commit_all "$WORKSPACE_ROOT/repos/orphan"
mkdir -p "$TMP/check-tmp"
cat >"$TMP/orphan.py" <<'PY'
"""orphan.py ROOT [aios-check args...]: start aios-check on ROOT's slow test, drop its stdout/stderr readers
(what sshd does when the client disappears), then require that the test stops, the helper cleans up and the
next check gets the lock at once."""
import json, os, subprocess, sys, time

root, args = sys.argv[1], sys.argv[2:]


def sleepers():
    found = []
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                if fh.read().split(b"\0")[:2] == [b"sleep", b"4242"]:
                    found.append(pid)
        except OSError:
            pass
    return found


def wait_for(cond, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.1)
    return False


open(os.path.join(root, "slow.flag"), "w").close()
proc = subprocess.Popen(["aios-check", *args, root], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE)
assert wait_for(sleepers, 30), "the slow test never started"
proc.stdout.close()
proc.stderr.close()
t0 = time.monotonic()
assert wait_for(lambda: not sleepers(), 20), "the test command outlived its client"
proc.wait(timeout=20)
stopped = time.monotonic() - t0
os.remove(os.path.join(root, "slow.flag"))
leftovers = [n for n in os.listdir(os.environ["TMPDIR"]) if n.startswith("aios-check.")]
assert not leftovers, f"cleanup did not run: {leftovers}"
out = subprocess.run(["aios-check", "--json", root], capture_output=True, text=True, timeout=60,
                     env={**os.environ, "AIOS_CHECK_TIMEOUT": "20"})
d = json.loads(out.stdout.splitlines()[-1])
assert d["exit_code"] == 0 and not d["timed_out"] and "quick" in d["output_tail"], d
print(f"stopped {stopped:.1f}s after the client left")
PY
run env TMPDIR="$TMP/check-tmp" python3 -I "$TMP/orphan.py" "$WORKSPACE_ROOT/repos/orphan" --json
check "--json: client gone -> test stopped, cleanup ran, lock free for the next check" bash -c '[[ $1 == 0 && $2 == stopped* ]]' _ "$RC" "$OUT"
run env TMPDIR="$TMP/check-tmp" python3 -I "$TMP/orphan.py" "$WORKSPACE_ROOT/repos/orphan"
check "human mode: client gone -> test stopped, cleanup ran, lock free for the next check" bash -c '[[ $1 == 0 && $2 == stopped* ]]' _ "$RC" "$OUT"
run aios-check "$WORKSPACE_ROOT/repos/orphan"
check "human mode still streams output and summarises on stderr" bash -c '[[ $1 == 0 && $2 == *quick* && $3 == *"PASS exit=0"* ]]' _ "$RC" "$OUT" "$ERR"

# --- Python
UV_PYTEST=0
if command -v uv >/dev/null 2>&1 && timeout 180 uv run --quiet --no-project --with pytest python -c 'import pytest' >/dev/null 2>&1; then
  UV_PYTEST=1
fi
if ((UV_PYTEST)); then
  fixture_py "$WORKSPACE_ROOT/repos/py-pass" pass dev
  run aios-check --json "$WORKSPACE_ROOT/repos/py-pass/tests/test_calc.py"
  jcheck "pytest project (dev group) via uv -> exit 0" 'd["exit_code"] == 0 and d["command"] == "uv run pytest -q" and "1 passed" in d["output_tail"]'
  check "uv.lock created by the check is removed (repo had none)" test ! -e "$WORKSPACE_ROOT/repos/py-pass/uv.lock"
  check "check leaves the git status clean" test -z "$(git -C "$WORKSPACE_ROOT/repos/py-pass" status --porcelain)"
  fixture_py "$WORKSPACE_ROOT/repos/py-fail" fail none
  run aios-check --json "$WORKSPACE_ROOT/repos/py-fail/src/calc/__init__.py"
  jcheck "pytest project without pytest dep via uv --with -> exit 1" \
    'd["exit_code"] == 1 and d["command"] == "uv run --with pytest pytest -q" and "1 failed" in d["output_tail"]'
  new_repo "$WORKSPACE_ROOT/repos/py-empty"
  printf '[project]\nname = "e"\nversion = "0"\nrequires-python = ">=3.10"\n' >"$WORKSPACE_ROOT/repos/py-empty/pyproject.toml"
  commit_all "$WORKSPACE_ROOT/repos/py-empty"
  run aios-check --json "$WORKSPACE_ROOT/repos/py-empty"
  jcheck "pytest collected no tests -> skipped (exit 5)" 'd["skipped"] is True and d["exit_code"] == 5 and "no tests" in d["reason"]'

  # Standard layout, tests/ with conftest.py + pytest.ini (weak markers), a runtime dependency: the project
  # root is tested with its dependencies, never tests/ in an empty environment.
  py=$WORKSPACE_ROOT/repos/py-layout
  new_repo "$py"
  mkdir -p "$py/src/calc" "$py/tests"
  printf '[project]\nname = "calc"\nversion = "0.1.0"\nrequires-python = ">=3.10"\ndependencies = ["six"]\n\n[dependency-groups]\ndev = ["pytest"]\n\n[tool.pytest.ini_options]\npythonpath = ["src"]\n' >"$py/pyproject.toml"
  printf 'import six\n\n\ndef add(a, b):\n    return a + b\n' >"$py/src/calc/__init__.py"
  printf '' >"$py/tests/conftest.py"
  printf '[pytest]\n' >"$py/tests/pytest.ini"
  printf 'from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n' >"$py/tests/test_calc.py"
  printf '.venv/\n' >"$py/.gitignore"
  commit_all "$py"
  run aios-check --json "$py/tests/test_calc.py"
  jcheck "tests/ with conftest.py + pytest.ini -> project root with its dependencies" \
    'd["exit_code"] == 0 and d["root"].endswith("/py-layout") and d["command"] == "uv run pytest -q" and "1 passed" in d["output_tail"]'

  # requirements.txt app (no pyproject), flat layout, tests/conftest.py.
  py=$WORKSPACE_ROOT/repos/py-reqapp
  new_repo "$py"
  mkdir -p "$py/tests"
  printf 'six\n' >"$py/requirements.txt"
  printf 'import six\n\n\ndef hello():\n    return "hi"\n' >"$py/app.py"
  printf '' >"$py/tests/conftest.py"
  printf 'from app import hello\n\n\ndef test_hello():\n    assert hello() == "hi"\n' >"$py/tests/test_app.py"
  commit_all "$py"
  run aios-check --json "$py/tests/test_app.py"
  jcheck "requirements.txt app -> root, requirements installed, python -m pytest (root on sys.path)" \
    'd["exit_code"] == 0 and d["root"].endswith("/py-reqapp") and d["command"] == "uv run --no-project --with pytest --with-requirements requirements.txt python -m pytest -q"'

  # pyproject.toml with tool config only; dependencies in requirements.txt.
  py=$WORKSPACE_ROOT/repos/py-toolonly
  new_repo "$py"
  mkdir -p "$py/tests"
  printf '[tool.ruff]\nline-length = 100\n' >"$py/pyproject.toml"
  printf 'six\n' >"$py/requirements.txt"
  printf 'import six\n\n\ndef test_six():\n    assert six.PY3\n' >"$py/tests/test_six.py"
  commit_all "$py"
  run aios-check --json "$py/tests/test_six.py"
  jcheck "tool-only pyproject + requirements.txt -> requirements installed" \
    'd["exit_code"] == 0 and d["root"].endswith("/py-toolonly") and "--with-requirements requirements.txt" in d["command"] and "1 passed" in d["output_tail"]'

  # PEP 621 project without pytest; test dependencies in requirements-dev.txt.
  py=$WORKSPACE_ROOT/repos/py-reqdev
  new_repo "$py"
  printf '[project]\nname = "r"\nversion = "0"\nrequires-python = ">=3.10"\n' >"$py/pyproject.toml"
  printf 'six\n' >"$py/requirements-dev.txt"
  printf 'import six\n\n\ndef test_six():\n    assert six.PY3\n' >"$py/test_six.py"
  printf '.venv/\n' >"$py/.gitignore"
  commit_all "$py"
  run aios-check --json "$py/test_six.py"
  jcheck "project without pytest + requirements-dev.txt -> requirements layered on uv run" \
    'd["exit_code"] == 0 and d["command"] == "uv run --with pytest --with-requirements requirements-dev.txt pytest -q"'
  check "no uv.lock left behind" test -z "$(git -C "$py" status --porcelain --untracked-files=all)"

  # PEP 621 project with pytest in its dev group, runtime dependencies only in requirements.txt.
  py=$WORKSPACE_ROOT/repos/py-devreq
  new_repo "$py"
  printf '[project]\nname = "d"\nversion = "0"\nrequires-python = ">=3.10"\n\n[dependency-groups]\ndev = ["pytest"]\n' >"$py/pyproject.toml"
  printf 'six\n' >"$py/requirements.txt"
  printf 'import six\n\n\ndef test_six():\n    assert six.PY3\n' >"$py/test_six.py"
  commit_all "$py"
  run aios-check --json "$py/test_six.py"
  jcheck "project (pytest in dev group) + requirements.txt -> requirements layered on uv run" \
    'd["exit_code"] == 0 and d["command"] == "uv run --with-requirements requirements.txt pytest -q" and "1 passed" in d["output_tail"]'

  # Poetry (no [project] table): editable install via poetry-core + its dev group, constraints converted.
  py=$WORKSPACE_ROOT/repos/py-poetry
  new_repo "$py"
  mkdir -p "$py/src/mypkg" "$py/tests"
  printf '[tool.poetry]\nname = "mypkg"\nversion = "0.1.0"\ndescription = ""\nauthors = ["Dev <dev@example.test>"]\npackages = [{ include = "mypkg", from = "src" }]\n\n[tool.poetry.dependencies]\npython = "^3.10"\nsix = "^1.16"\n\n[tool.poetry.group.dev.dependencies]\npytest = "^8.0"\npytest-mock = "^3.10"\n\n[build-system]\nrequires = ["poetry-core>=1.0.0"]\nbuild-backend = "poetry.core.masonry.api"\n' >"$py/pyproject.toml"
  printf 'import six\n\n\ndef f():\n    return six.__name__\n' >"$py/src/mypkg/__init__.py"
  printf 'from mypkg import f\n\n\ndef test_f(mocker):\n    assert f() == "six"\n' >"$py/tests/test_f.py"
  commit_all "$py"
  run aios-check --json "$py/tests/test_f.py"
  jcheck "Poetry project -> editable install + dev group (pytest-mock) -> exit 0" \
    'd["exit_code"] == 0 and d["command"] == "uv run --no-project --with pytest --with-editable . --with '"'pytest>=8.0,<9'"' --with '"'pytest-mock>=3.10,<4'"' python -m pytest -q" and "1 passed" in d["output_tail"]'
  printf '\n[tool.poetry.group.test.dependencies]\nhelper = { git = "https://git.example.test/helper.git" }\n' >>"$py/pyproject.toml"
  run aios-check --json "$py/tests/test_f.py"
  jcheck "Poetry git dependency -> skipped with a reason, not a fabricated failure" \
    'd["skipped"] is True and "only poetry" in d["reason"] and "helper" in d["reason"]'

  # uv workspace: the member is tested, but uv writes uv.lock at the workspace root.
  py=$WORKSPACE_ROOT/repos/py-workspace
  new_repo "$py"
  mkdir -p "$py/packages/a/src/a" "$py/packages/a/tests"
  printf '[project]\nname = "root"\nversion = "0"\nrequires-python = ">=3.10"\n\n[tool.uv.workspace]\nmembers = ["packages/*"]\n' >"$py/pyproject.toml"
  printf '[project]\nname = "a"\nversion = "0.1.0"\nrequires-python = ">=3.10"\n\n[dependency-groups]\ndev = ["pytest"]\n\n[tool.pytest.ini_options]\npythonpath = ["src"]\n' >"$py/packages/a/pyproject.toml"
  printf 'def one():\n    return 1\n' >"$py/packages/a/src/a/__init__.py"
  printf 'from a import one\n\n\ndef test_one():\n    assert one() == 1\n' >"$py/packages/a/tests/test_one.py"
  commit_all "$py"
  run aios-check --json "$py/packages/a/src/a/__init__.py"
  jcheck "uv workspace member -> member tested" 'd["exit_code"] == 0 and d["root"].endswith("/packages/a") and "1 passed" in d["output_tail"]'
  check "uv workspace: uv.lock created at the workspace root is removed (git status clean)" \
    test -z "$(git -C "$py" status --porcelain --untracked-files=all)"
else
  skip "Python projects via uv" "uv missing or cannot provision pytest (offline?)"
fi

# Fallback without uv: python3 -m pytest from a venv that has pytest.
NO_UV_PATH="$BIN:/usr/local/bin:/usr/bin:/bin"
if PATH=$NO_UV_PATH command -v uv >/dev/null 2>&1; then
  skip "python3 -m pytest fallback" "uv is installed system-wide, cannot hide it"
else
  venv=$TMP/venv
  make_venv() {
    if command -v uv >/dev/null 2>&1; then
      timeout 180 uv venv --quiet --python "$(command -v python3)" "$venv" &&
        timeout 180 uv pip install --quiet --python "$venv/bin/python" pytest
    else
      python3 -m venv "$venv" && timeout 180 "$venv/bin/python" -m pip install --quiet pytest
    fi
  }
  if make_venv >/dev/null 2>&1 && "$venv/bin/python3" -c 'import pytest' 2>/dev/null; then
    fixture_py "$WORKSPACE_ROOT/repos/py-fallback" pass none
    run env PATH="$venv/bin:$NO_UV_PATH" aios-check --json "$WORKSPACE_ROOT/repos/py-fallback/tests"
    jcheck "no uv: python3 -m pytest fallback -> exit 0" 'd["exit_code"] == 0 and d["command"] == "python3 -m pytest -q" and "1 passed" in d["output_tail"]'
  else
    skip "python3 -m pytest fallback" "could not create a venv with pytest (offline?)"
  fi
  if ! PATH=$NO_UV_PATH python3 -I -c 'import pytest' 2>/dev/null; then
    fixture_py "$WORKSPACE_ROOT/repos/py-norunner" pass none
    run env PATH="$NO_UV_PATH" aios-check --json "$WORKSPACE_ROOT/repos/py-norunner"
    jcheck "no uv and no pytest -> skipped with reason" 'd["skipped"] is True and "neither uv nor pytest" in d["reason"]'
  fi
fi

# --- Node
if ((NODE_OK)); then
  new_repo "$WORKSPACE_ROOT/repos/node-pass"
  printf '{"name":"n","version":"1.0.0","scripts":{"test":"node -e \\"console.log(42)\\""}}\n' >"$WORKSPACE_ROOT/repos/node-pass/package.json"
  commit_all "$WORKSPACE_ROOT/repos/node-pass"
  run aios-check --json "$WORKSPACE_ROOT/repos/node-pass/package.json"
  jcheck "package.json test script, no deps -> npm test" 'd["exit_code"] == 0 and d["command"] == "npm test" and "42" in d["output_tail"]'

  new_repo "$WORKSPACE_ROOT/repos/node-deps"
  mkdir -p "$WORKSPACE_ROOT/repos/node-deps/dep"
  printf '{"name":"dep","version":"1.0.0","main":"index.js"}\n' >"$WORKSPACE_ROOT/repos/node-deps/dep/package.json"
  printf 'module.exports = () => 41;\n' >"$WORKSPACE_ROOT/repos/node-deps/dep/index.js"
  printf '{"name":"nd","version":"1.0.0","dependencies":{"dep":"file:./dep"},"scripts":{"test":"node -e \\"process.exit(require(\x27dep\x27)() === 41 ? 1 : 0)\\""}}\n' \
    >"$WORKSPACE_ROOT/repos/node-deps/package.json"
  commit_all "$WORKSPACE_ROOT/repos/node-deps"
  run aios-check --json "$WORKSPACE_ROOT/repos/node-deps"
  jcheck "deps without node_modules -> npm install first; failing script -> exit 1" \
    'd["exit_code"] == 1 and d["command"] == "npm install --no-audit --no-fund && npm test"'
  check "package-lock.json created by the install is removed, node_modules ignored" \
    bash -c '[[ ! -e $1/package-lock.json && -d $1/node_modules && -z $(git -C "$1" status --porcelain) ]]' _ "$WORKSPACE_ROOT/repos/node-deps"

  new_repo "$WORKSPACE_ROOT/repos/node-placeholder"
  printf '{"name":"p","scripts":{"test":"echo \\"Error: no test specified\\" && exit 1"}}\n' >"$WORKSPACE_ROOT/repos/node-placeholder/package.json"
  commit_all "$WORKSPACE_ROOT/repos/node-placeholder"
  run aios-check --json "$WORKSPACE_ROOT/repos/node-placeholder"
  jcheck "npm init placeholder test script -> skipped" 'd["skipped"] is True'
else
  skip "Node projects" "node/npm not installed"
fi

# --- contract with the Hermes plugin: tools/hermes-plugin/aios/checks.py run_check over a fake ssh
CHECKS_PY=$REPO/tools/hermes-plugin/aios/checks.py
if [[ -f $CHECKS_PY ]] && ((GO_OK)); then
  mkdir -p "$TMP/fakessh"
  cat >"$TMP/fakessh/ssh" <<'EOF'
#!/usr/bin/env bash
# fake ssh: run the remote command (last argument) locally, like `ssh agent@sandbox <cmd>` would
exec bash -c "${!#}"
EOF
  chmod +x "$TMP/fakessh/ssh"
  run env PATH="$TMP/fakessh:$PATH" TERMINAL_SSH_HOST=sandbox AIOS_CHECK_BIN="$SANDBOX/bin/aios-check" \
    python3 -I - "$CHECKS_PY" \
    "$WORKSPACE_ROOT/repos/go-pass/calc/add.go" "$WORKSPACE_ROOT/repos/go-fail/calc/add.go" \
    "$WORKSPACE_ROOT/repos/no-tests/README.md" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("aios_checks", sys.argv[1])
checks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checks)
ok, bad, none = (checks.run_check(p) for p in sys.argv[2:5])
assert ok and ok["exit_code"] == 0 and ok["command"] == "go test ./...", ok
assert bad and bad["exit_code"] == 1 and "FAIL" in bad["output_tail"], bad
assert none is None, none  # skipped -> no evidence
print("contract ok")
PY
  check "plugin run_check parses aios-check (pass, fail, skipped->None)" test "$OUT" = "contract ok"
else
  skip "plugin contract" "checks.py or go missing"
fi

# --- end to end over a real, unprivileged sshd running this sshd_config (only non-root overrides added)
section "end-to-end over sshd"
if [[ -n $SSHD && -x $SSHD && $(id -u) != 0 && -f $CHECKS_PY ]] && ((GO_OK)); then
  e2e=$TMP/e2e
  mkdir -p "$e2e/conf.d" "$e2e/home" "$e2e/shim"
  ssh-keygen -q -t ed25519 -N '' -f "$e2e/hostkey"
  ssh-keygen -q -t ed25519 -N '' -f "$e2e/client"
  cp "$e2e/client.pub" "$e2e/authorized_keys"
  sed "s#/etc/ssh/sshd_config.d/\*.conf#$e2e/conf.d/*.conf#" "$SANDBOX/sshd_config" >"$e2e/sshd_config"
  # What the entrypoint generates, plus HOME so nothing lands in the real home directory.
  printf 'SetEnv HOME=%s WORKSPACE_ROOT=%s AIOS_CHECK_TIMEOUT=120 PATH=%s\n' "$e2e/home" "$WORKSPACE_ROOT" "$PATH" >"$e2e/conf.d/00-aios-env.conf"
  for tool in ssh scp; do # keep host keys out of ~/.ssh/known_hosts
    printf '#!/bin/sh\nexec %s -o UserKnownHostsFile=%s "$@"\n' "$(command -v "$tool")" "$e2e/known_hosts" >"$e2e/shim/$tool"
    chmod +x "$e2e/shim/$tool"
  done
  e2e_port=$(python3 -I -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
  me=$(id -un)
  env ${SANDBOX_SSHD_LIBS:+LD_LIBRARY_PATH="$SANDBOX_SSHD_LIBS"} "$SSHD" -D -e -f "$e2e/sshd_config" -p "$e2e_port" \
    -o ListenAddress=127.0.0.1 -o "HostKey=$e2e/hostkey" -o PidFile=none -o "AllowUsers=$me" \
    -o "AuthorizedKeysFile=$e2e/authorized_keys" -o StrictModes=no "${sshd_extra[@]}" 2>"$e2e/sshd.log" &
  SSHD_PID=$!
  for _ in $(seq 1 50); do aios-healthcheck "$e2e_port" && break; sleep 0.1; done
  E2E_SSH=("$e2e/shim/ssh" -p "$e2e_port" -i "$e2e/client" -o BatchMode=yes -o StrictHostKeyChecking=accept-new)
  if aios-healthcheck "$e2e_port"; then
    run env PATH="$e2e/shim:$PATH" TERMINAL_SSH_HOST=127.0.0.1 TERMINAL_SSH_PORT="$e2e_port" TERMINAL_SSH_USER="$me" \
      AIOS_CHECK_BIN="$SANDBOX/bin/aios-check" \
      TERMINAL_SSH_KEY="$e2e/client" python3 -I - "$CHECKS_PY" "$WORKSPACE_ROOT/repos/go-fail/calc/add.go" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("aios_checks", sys.argv[1])
checks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checks)
r = checks.run_check(sys.argv[2])
assert r and r["exit_code"] == 1 and "FAIL" in r["output_tail"] and r["command"] == "go test ./...", r
print("ssh ok")
PY
    check "plugin run_check over real ssh -> failing go test evidence" test "$OUT" = "ssh ok"
    run env GITHUB_TOKEN=ghp_sent EDGE_API_KEY=ek_sent HERMES_TENANT=pessoal FOO=bar "${E2E_SSH[@]}" \
      -o SendEnv=GITHUB_TOKEN -o SendEnv=EDGE_API_KEY -o SendEnv=HERMES_TENANT -o SendEnv=FOO "$me@127.0.0.1" \
      'printf "%s|%s|%s|%s|%s" "${GITHUB_TOKEN:-unset}" "${EDGE_API_KEY:-unset}" "${HERMES_TENANT:-unset}" "${FOO:-unset}" "$WORKSPACE_ROOT"'
    check "sessions get SetEnv; AcceptEnv admits only GITHUB_TOKEN, EDGE_API_KEY, HERMES_TENANT" \
      test "$OUT" = "ghp_sent|ek_sent|pessoal|unset|$WORKSPACE_ROOT"
    printf 'payload\n' >"$e2e/up.txt"
    run "$e2e/shim/scp" -q -P "$e2e_port" -i "$e2e/client" -o BatchMode=yes "$e2e/up.txt" "$me@127.0.0.1:$e2e/home/up.txt"
    check "scp works through internal-sftp (Hermes file sync)" cmp -s "$e2e/up.txt" "$e2e/home/up.txt"
    fwd_port=$(python3 -I -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
    "${E2E_SSH[@]}" -N -L "127.0.0.1:$fwd_port:127.0.0.1:$e2e_port" "$me@127.0.0.1" 2>"$e2e/fwd.log" &
    fwd_pid=$!
    for _ in $(seq 1 50); do aios-healthcheck "$fwd_port" && break; sleep 0.1; done
    python3 -I -c 'import socket, sys; s = socket.create_connection(("127.0.0.1", int(sys.argv[1])), 5); s.settimeout(5); s.recv(64)' "$fwd_port" 2>/dev/null
    sleep 0.3
    kill "$fwd_pid" 2>/dev/null
    wait "$fwd_pid" 2>/dev/null
    check "TCP forwarding is refused by the server" grep -q 'administratively prohibited' "$e2e/fwd.log"
    # Hermes' terminal timeout: it SIGKILLs its ssh mux client, the ControlMaster connection (and its
    # sshd-session) survives and no SIGHUP is sent without a pty. The remote check must still stop.
    cm=$e2e/cm.sock
    CM_SSH=("${E2E_SSH[@]}" -o ControlMaster=auto -o "ControlPath=$cm" -o ControlPersist=60)
    "${CM_SSH[@]}" "$me@127.0.0.1" true
    touch "$WORKSPACE_ROOT/repos/orphan/slow.flag"
    "${CM_SSH[@]}" "$me@127.0.0.1" "aios-check --json $WORKSPACE_ROOT/repos/orphan" >/dev/null 2>&1 &
    mux_client=$!
    for _ in $(seq 1 100); do pgrep -f '^sleep 4242$' >/dev/null && break; sleep 0.2; done
    started=$(pgrep -f '^sleep 4242$' | wc -l)
    kill -KILL "$mux_client" 2>/dev/null
    wait "$mux_client" 2>/dev/null
    for _ in $(seq 1 100); do pgrep -f '^sleep 4242$' >/dev/null || break; sleep 0.2; done
    left=$(pgrep -f '^sleep 4242$' | wc -l)
    master_alive=0
    "${CM_SSH[@]}" -O check "$me@127.0.0.1" 2>/dev/null && master_alive=1
    rm -f "$WORKSPACE_ROOT/repos/orphan/slow.flag"
    run "${CM_SSH[@]}" "$me@127.0.0.1" "AIOS_CHECK_TIMEOUT=20 aios-check --json $WORKSPACE_ROOT/repos/orphan"
    jcheck "ControlMaster: mux client killed (master alive=$master_alive) -> remote test stopped, next check not blocked" \
      "$started >= 1 and $left == 0 and d['exit_code'] == 0 and not d['timed_out']"
    "${CM_SSH[@]}" -O exit "$me@127.0.0.1" 2>/dev/null
  else
    fail "unprivileged sshd did not start" "$(cat "$e2e/sshd.log")"
  fi
  kill "$SSHD_PID" 2>/dev/null
  wait "$SSHD_PID" 2>/dev/null
else
  skip "end-to-end over sshd" "needs SANDBOX_SSHD_BIN (non-root), go and the plugin's checks.py"
fi

# =========================================================================== task lifecycle
section "aios-task-start / check / patch / destroy"
if ((GO_OK)); then
  fixture_go "$TMP/fixtures/gomod" pass
  git -C "$TMP/fixtures/gomod" tag v1
  git -C "$TMP/fixtures/gomod" switch -q -c feature
  printf 'feature\n' >"$TMP/fixtures/gomod/FEATURE"
  commit_all "$TMP/fixtures/gomod" feature
  git -C "$TMP/fixtures/gomod" switch -q main
  git clone -q --bare "$TMP/fixtures/gomod" "$TMP/remotes/gomod.git"
  main_sha=$(git -C "$TMP/fixtures/gomod" rev-parse main)
  feature_sha=$(git -C "$TMP/fixtures/gomod" rev-parse feature)
  URL=https://git.example.test/gomod.git

  run aios-task-start T1 "$URL"
  T1=$WORKSPACE_ROOT/tasks/T1
  check "start: prints the task dir" test "$OUT" = "$T1"
  check "start: branch aios/T1 at the default branch, base recorded" bash -c \
    '[[ $(git -C "$1" symbolic-ref --short HEAD) == aios/T1 && $(git -C "$1" rev-parse HEAD) == "$2" && $(git -C "$1" config aios.base) == "$2" ]]' _ "$T1" "$main_sha"
  run aios-task-start T1 "$URL"
  check "start: same id + url is idempotent" bash -c '[[ $1 == 0 && $2 == "$3" ]]' _ "$RC" "$OUT" "$T1"
  run aios-task-start T1 https://git.example.test/other.git
  check "start: same id, different url -> error" test "$RC" -eq 1

  run aios-task-start T-tag "$URL" v1
  check "start: ref = tag" bash -c '[[ $(git -C "$1" rev-parse HEAD) == "$2" ]]' _ "$OUT" "$main_sha"
  run aios-task-start T_branch "$URL" feature
  check "start: ref = remote branch" bash -c '[[ $(git -C "$1" rev-parse HEAD) == "$2" && -f $1/FEATURE ]]' _ "$OUT" "$feature_sha"
  run aios-task-start Tsha "$URL" "$feature_sha"
  check "start: ref = commit sha" bash -c '[[ $(git -C "$1" rev-parse HEAD) == "$2" && $(git -C "$1" symbolic-ref --short HEAD) == aios/Tsha ]]' _ "$OUT" "$feature_sha"
  run aios-task-start Tbad "$URL" no-such-ref
  check "start: unknown ref -> error, nothing left behind" bash -c \
    '[[ $1 != 0 && ! -e $2/tasks/Tbad && -z $(compgen -G "$2/tasks/.Tbad.tmp.*") ]]' _ "$RC" "$WORKSPACE_ROOT"
  run aios-task-start Tmissing https://git.example.test/missing.git
  check "start: clone failure -> error, nothing left behind" bash -c \
    '[[ $1 == 1 && ! -e $2/tasks/Tmissing && -z $(compgen -G "$2/tasks/.Tmissing.tmp.*") ]]' _ "$RC" "$WORKSPACE_ROOT"

  for bad_id in "../x" "a b" "x/y" "$(printf 'a%.0s' {1..65})" ""; do
    run aios-task-start "$bad_id" "$URL"
    check "start: rejects id '${bad_id:0:12}'" test "$RC" -eq 2
  done
  for bad_url in "file://$TMP/remotes/gomod.git" "$TMP/remotes/gomod.git" "http://git.example.test/gomod.git" \
    "ext::sh -c touch% $TMP/pwned" "https://user:secret@git.example.test/gomod.git" "-uhttps://x/y" \
    "git@-oProxyCommand=touch:x" "https://git.example.test/a b"; do
    run aios-task-start Turl "$bad_url"
    check "start: rejects url '${bad_url:0:40}'" bash -c '[[ $1 == 2 && ! -e $2 ]]' _ "$RC" "$WORKSPACE_ROOT/tasks/Turl"
  done
  check "start: no command injection via url" test ! -e "$TMP/pwned"

  run aios-task-check T1
  jcheck "task-check: detected tests" 'd["exit_code"] == 0 and d["command"] == "go test ./..." and d["root"].endswith("/tasks/T1")'
  run aios-task-check T1 'echo hello-from-task && exit 3'
  jcheck "task-check: shell command line" 'd["exit_code"] == 3 and "hello-from-task" in d["output_tail"] and d["command"] == "echo hello-from-task && exit 3"'
  check "task-check: exit status mirrors the command" test "$RC" -eq 3
  run aios-task-check T1 go vet ./...
  jcheck "task-check: argv command" 'd["exit_code"] == 0 and d["command"] == "go vet ./..."'
  run aios-task-check nope
  jcheck "task-check: unknown task -> skipped JSON" 'd["skipped"] is True and "not found" in d["reason"]'
  check "task-check: unknown task exits 2" test "$RC" -eq 2
  run aios-task-check '../etc'
  check "task-check: invalid id exits 2" test "$RC" -eq 2
  run env AIOS_TASK_CHECK_TIMEOUT=2 aios-task-check T1 'echo begun; sleep 30'
  jcheck "task-check: AIOS_TASK_CHECK_TIMEOUT bounds the run -> exit 124, timed_out" \
    'd["exit_code"] == 124 and d["timed_out"] is True and "timed out after 2s" in d["output_tail"] and "begun" in d["output_tail"]'
  run env AIOS_CHECK_TIMEOUT=1 aios-task-check T1 'sleep 2; echo slow-ok'
  jcheck "task-check: its budget is AIOS_TASK_CHECK_TIMEOUT (280), not the plugin's AIOS_CHECK_TIMEOUT" \
    'd["exit_code"] == 0 and "slow-ok" in d["output_tail"]'

  # Relative paths (as Hermes reports them) resolve inside the task that has them.
  run bash -c 'cd "$HOME" && aios-check --json calc/add.go'
  jcheck "relative changed path resolves under WORKSPACE_ROOT/tasks/*" 'd["exit_code"] == 0 and "/tasks/" in d["root"]'

  # Patch: committed + modified + deleted + untracked (text and binary) changes; artifacts excluded.
  printf 'package calc\n\n// Sub subtracts.\nfunc Sub(a, b int) int { return a - b }\n' >"$T1/calc/sub.go"
  git -C "$T1" add calc/sub.go && git -C "$T1" commit -qm "add Sub"
  printf 'package calc\n\n// Add adds.\nfunc Add(a, b int) int { return a + b }\n' >"$T1/calc/add.go"
  rm "$T1/remove-me.txt"
  printf 'brand new\n' >"$T1/NEW.md"
  printf '\x00\x01\x02binary\xff' >"$T1/blob.bin"
  mkdir -p "$T1/.venv/lib" "$T1/node_modules/x"
  printf 'junk\n' >"$T1/.venv/lib/junk.py"
  printf 'junk\n' >"$T1/node_modules/x/index.js"
  status_before=$(git -C "$T1" status --porcelain)
  run aios-task-patch T1
  patch=$WORKSPACE_ROOT/patches/T1.patch
  check "patch: prints the patch path" test "$OUT" = "$patch"
  check "patch: committed, modified, deleted, untracked and binary changes" bash -c \
    'for f in calc/sub.go calc/add.go remove-me.txt NEW.md blob.bin; do grep -q "^diff --git a/$f b/$f" "$1" || exit 1; done; grep -q "GIT binary patch" "$1"' _ "$patch"
  check "patch: .venv and node_modules excluded" bash -c '! grep -Eq "^diff --git a/(\.venv|node_modules)/" "$1"' _ "$patch"
  check "patch: task index untouched" test "$(git -C "$T1" status --porcelain)" = "$status_before"
  git clone -q "$TMP/remotes/gomod.git" "$TMP/verify"
  run git -C "$TMP/verify" apply --index "$patch"
  check "patch: applies cleanly on the base commit and reproduces the task tree" bash -c \
    '[[ $1 == 0 ]] && diff -r -q --exclude=.git --exclude=.venv --exclude=node_modules "$2" "$3"' _ "$RC" "$TMP/verify" "$T1"
  run aios-task-patch T-tag
  check "patch: no changes -> empty patch file" bash -c '[[ $1 == 0 && -f $2 && ! -s $2 ]]' _ "$RC" "$WORKSPACE_ROOT/patches/T-tag.patch"
  run aios-task-patch nope
  check "patch: unknown task -> error" test "$RC" -eq 1

  mkdir -p "$T1/ro/deep"
  printf 'x\n' >"$T1/ro/deep/file"
  chmod 0555 "$T1/ro/deep" "$T1/ro"
  run aios-task-destroy T1
  check "destroy: removes the task, even read-only dirs" bash -c '[[ $1 == 0 && ! -e $2 ]]' _ "$RC" "$T1"
  check "destroy: keeps the patch" test -s "$patch"
  run aios-task-destroy T1
  check "destroy: idempotent" test "$RC" -eq 0
  run aios-task-destroy '../ws'
  check "destroy: invalid id exits 2" bash -c '[[ $1 == 2 && -d $2 ]]' _ "$RC" "$WORKSPACE_ROOT"
  run env WORKSPACE_ROOT="$TMP/fresh-ws" aios-task-destroy T9
  check "destroy: fresh workspace is fine" test "$RC" -eq 0
  run env WORKSPACE_ROOT=/ aios-task-destroy T9
  check "WORKSPACE_ROOT=/ is refused" test "$RC" -eq 1
else
  skip "task lifecycle" "go not installed"
fi

# Empty repository: the base is the empty tree, so the agent's commits are part of the patch.
git init -q --bare "$TMP/remotes/empty.git"
run aios-task-start Tempty https://git.example.test/empty.git
TE=$WORKSPACE_ROOT/tasks/Tempty
check "start: empty repository -> aios.base is the empty tree" bash -c \
  '[[ $1 == 0 && $(git -C "$2" config aios.base) == $(git -C "$2" hash-object -t tree /dev/null) ]]' _ "$RC" "$TE"
printf 'a\n' >"$TE/a.txt"
git -C "$TE" add a.txt && git -C "$TE" commit -qm "add a"
printf 'b\n' >"$TE/b.txt"
run aios-task-patch Tempty
patch_empty=$WORKSPACE_ROOT/patches/Tempty.patch
check "patch: empty repository -> committed and untracked files" bash -c \
  '[[ $1 == 0 ]] && grep -q "^diff --git a/a.txt b/a.txt" "$2" && grep -q "^diff --git a/b.txt b/b.txt" "$2"' _ "$RC" "$patch_empty"
git clone -q "$TMP/remotes/empty.git" "$TMP/verify-empty" 2>/dev/null
run git -C "$TMP/verify-empty" apply --index "$patch_empty"
check "patch: applies to a fresh clone of the empty repository" bash -c '[[ $1 == 0 && -f $2/a.txt && -f $2/b.txt ]]' _ "$RC" "$TMP/verify-empty"
git -C "$TE" config --unset aios.base
run aios-task-patch Tempty
check "patch: task without aios.base (started before the fix) still includes its commits" \
  grep -q "^diff --git a/a.txt b/a.txt" "$patch_empty"
run aios-task-destroy Tempty

# =========================================================================== git credential helper
section "aios-git-credential"
cred() { printf 'protocol=%s\nhost=%s\n\n' "$1" "$2" | aios-git-credential get; }
run env GITHUB_TOKEN=ghp_envtoken bash -c "$(declare -f cred); cred https github.com"
check "env token for https://github.com" test "$OUT" = $'username=x-access-token\npassword=ghp_envtoken'
run env GITHUB_TOKEN=ghp_envtoken bash -c "$(declare -f cred); cred https gitlab.com"
check "never answers for other hosts" test -z "$OUT"
run env GITHUB_TOKEN=ghp_envtoken bash -c "$(declare -f cred); cred http github.com"
check "never answers over plain http" test -z "$OUT"
printf 'ghp_filetoken\n' >"$TMP/token"
run env AIOS_GITHUB_TOKEN_FILE="$TMP/token" bash -c "$(declare -f cred); cred https github.com"
check "falls back to the tmpfs token file" test "$OUT" = $'username=x-access-token\npassword=ghp_filetoken'
run env AIOS_GITHUB_TOKEN_FILE="$TMP/missing" bash -c "$(declare -f cred); cred https github.com"
check "no token -> no answer (anonymous)" bash -c '[[ $1 == 0 && -z $2 ]]' _ "$RC" "$OUT"
run bash -c 'printf "protocol=https\nhost=github.com\npassword=x\n\n" | GITHUB_TOKEN=t aios-git-credential store'
check "store is a no-op" bash -c '[[ $1 == 0 && -z $2 ]]' _ "$RC" "$OUT"
sed "s#/usr/local/bin/aios-git-credential#$BIN/aios-git-credential#" "$SANDBOX/gitconfig" >"$TMP/system-gitconfig"
run env GIT_CONFIG_NOSYSTEM=0 GIT_CONFIG_SYSTEM="$TMP/system-gitconfig" GITHUB_TOKEN=ghp_viagit \
  bash -c 'printf "protocol=https\nhost=github.com\npath=org/repo.git\n\n" | git credential fill'
check "system gitconfig wires the helper into git (credential fill)" bash -c '[[ $1 == *"password=ghp_viagit"* && $1 == *"username=x-access-token"* ]]' _ "$OUT"
run env GIT_CONFIG_NOSYSTEM=0 GIT_CONFIG_SYSTEM="$TMP/system-gitconfig" GITHUB_TOKEN=ghp_viagit \
  bash -c 'printf "protocol=https\nhost=example.com\n\n" | git credential fill'
check "git never gets the token for other hosts" bash -c '[[ $1 != *ghp_viagit* ]]' _ "$OUT"

# =========================================================================== healthcheck
section "aios-healthcheck"
python3 -I -c '
import socket, sys, time
s = socket.socket(); s.bind(("127.0.0.1", 0)); s.listen()
print(s.getsockname()[1], flush=True); time.sleep(60)' >"$TMP/port" &
LISTENER_PID=$!
for _ in $(seq 1 50); do [[ -s $TMP/port ]] && break; sleep 0.1; done
port=$(<"$TMP/port")
run aios-healthcheck "$port"
check "healthy while a socket listens on the port" test "$RC" -eq 0
kill "$LISTENER_PID" 2>/dev/null
wait "$LISTENER_PID" 2>/dev/null
LISTENER_PID=""
run aios-healthcheck "$port"
check "unhealthy once nothing listens" test "$RC" -eq 1
run aios-healthcheck 'x;y'
check "rejects an invalid port" test "$RC" -eq 2

# =========================================================================== make targets
section "mk/sandbox.mk"
run make --no-print-directory -f "$REPO/mk/sandbox.mk" sandbox-keys SANDBOX_KEYS_DIR="$TMP/keys" SANDBOX_WORKSPACE_DIR="$TMP/kws"
check "sandbox-keys creates the keypair, authorized_keys and workspace" bash -c \
  '[[ $1 == 0 && -s $2/id_ed25519 && -s $2/id_ed25519.pub && -d $3 ]] && cmp -s "$2/id_ed25519.pub" "$2/authorized_keys"' _ "$RC" "$TMP/keys" "$TMP/kws"
check "sandbox-keys permissions 600/644" bash -c \
  '[[ $(stat -c %a "$1/id_ed25519") == 600 && $(stat -c %a "$1/id_ed25519.pub") == 644 && $(stat -c %a "$1/authorized_keys") == 644 ]]' _ "$TMP/keys"
fp=$(ssh-keygen -lf "$TMP/keys/id_ed25519.pub")
run make --no-print-directory -f "$REPO/mk/sandbox.mk" sandbox-keys SANDBOX_KEYS_DIR="$TMP/keys" SANDBOX_WORKSPACE_DIR="$TMP/kws"
check "sandbox-keys is idempotent (same key, one authorized line)" bash -c \
  '[[ $1 == 0 && $(ssh-keygen -lf "$2/id_ed25519.pub") == "$3" && $(wc -l <"$2/authorized_keys") == 1 ]]' _ "$RC" "$TMP/keys" "$fp"
run make -n --no-print-directory -f "$REPO/mk/sandbox.mk" sandbox-build sandbox-shell DC="docker compose"
check "sandbox-build / sandbox-shell use the agent profile" bash -c \
  '[[ $1 == *"docker compose --profile agent build sandbox"* && $1 == *"exec -it -u agent -w /workspace sandbox env -u GITHUB_TOKEN bash -l"* ]]' _ "$OUT"

# =========================================================================== summary
printf '\n%d passed, %d failed, %d skipped\n' "$PASSED" "$FAILED" "$SKIPPED"
((FAILED == 0))
