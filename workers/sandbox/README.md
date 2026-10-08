# sandbox — isolated execution for Hermes

Implements docs/CONTRACTS.md §6 and ARCHITECTURE §21/§24.16. It is a container with `sshd`, a non-root user `agent` (uid/gid 1000), toolchains, and `/workspace`.

Every shell command an agent runs goes through Hermes' `terminal.backend: ssh` (`agent@sandbox:22`, see `config/hermes/config.yaml`). The `aios` plugin's `pre_verify` gate runs `ssh agent@sandbox aios-check --json <changed_path>`.

The sandbox is on its own network, `sandbox_net` (`172.30.1.0/24`), shared only with hermes and edge. It is not on `aios`. See Security.

The sandbox:
- makes no decisions;
- has no `docker.sock`;
- publishes no ports;
- mounts nothing from the host except `data/sandbox/*`.

```
Hermes ──ssh (key, ControlMaster)──▶ sandbox:22 ── bash -l ── /workspace/tasks/<id>  (clone → modify → test → patch → destroy)
   └─ aios pre_verify ──ssh──▶ aios-check --json <path>  → last stdout line = JSON evidence → decision /v1/gate
```

## Toolchain (pinned; amd64)

| Tool | Version | Source / verification |
|---|---|---|
| base | `debian:trixie-slim@sha256:a29215f6…` (= `trixie-20261005-slim`) | Docker Hub digest |
| OpenSSH | 10.0p1 (Debian `1:10.0p1-7+deb13u4`) | apt |
| git, make, build-essential, jq, ripgrep, fd (`fdfind` + `fd` link), sqlite3, python3 (3.13) + venv/dev, tzdata | trixie | apt |
| Go | 1.27.1 | go.dev tarball, sha256 `63d339f0…` |
| Node.js | 24.21.0 LTS (npm 11.19.0, corepack 0.36.0) | nodejs.org tarball, sha256 from the GPG-verified `SHASUMS256.txt` |
| pnpm | 12.10.1 | `corepack install -g` (signature-checked), in the agent's corepack cache |
| eslint / typescript (`tsc`) | 10.12.0 / 7.0.2 | `npm install -g` |
| uv / uvx | 0.12.23 | GitHub release tarball, sha256 |
| ruff | 0.16.10 | `uv tool install` (system python) |
| duckdb CLI | 1.5.6 | GitHub release `duckdb_cli-linux-amd64.gz`, sha256 |

Re-verify everything with `make sandbox-verify-downloads`, which re-downloads every artifact and compares its sha256. To bump a version, change its `ARG` and checksum together in the `Dockerfile`.

## Helpers (`/usr/local/bin`)

All helpers are bash with `set -euo pipefail`. `WORKSPACE_ROOT` defaults to `/workspace`; the tests point it at a temporary directory.

### `aios-check [--json] <path|dir>`

**Finding the project.** It starts from the path and walks up to the git toplevel. Outside git, it stops at `$WORKSPACE_ROOT`, or at `/` when the path is not under it. The first directory with a test command wins, so in a monorepo the nearest module is the one tested.

**How the path is resolved:**
- A deleted file resolves to its nearest existing parent.
- A relative path is tried against `$PWD`, then `$WORKSPACE_ROOT`, then the most recently modified match under `$WORKSPACE_ROOT/tasks/*`. This is needed because Hermes reports paths relative to the terminal cwd, which the plugin's separate SSH session doesn't share.

**Detection order, per directory:**

1. A Makefile `test` target → `make test`. The target is read from make's database, so a `test/` directory alone doesn't count.
2. `go.mod` → `go test ./...`
3. A `package.json` `test` script, ignoring the `npm init` placeholder → `pnpm test`, `yarn test`, `bun test` or `npm test`. The package manager comes from `packageManager` or the lockfile. If there are dependencies and no `node_modules`, it runs `pnpm install --frozen-lockfile`, `npm ci` or `npm install` first.
4. A pytest project. Markers come in two strengths:
   - **Installable root:** `setup.py`, or a `pyproject.toml` with `[project]`, `[tool.poetry]` or `[build-system]` (or one that doesn't parse, which uv then reports).
   - **Weak:** `setup.cfg`, `pytest.ini`, `tox.ini`, a requirements file (`requirements.txt`, `requirements-dev.txt`, `requirements-test.txt` and the usual variants), or a `pyproject.toml` with tool config only.
   - `conftest.py` is not a marker: `tests/conftest.py` doesn't make `tests/` a project.

   A directory found through weak markers defers to the nearest installable root above it, up to the git toplevel. Only when there is none is the weak directory tested. So `tests/test_x.py` in the standard layout tests the project with its dependencies, not `tests/` in an empty environment.

   Commands with uv:

   | Project | Command |
   |---|---|
   | `[project]` | `uv run [--group <g> \| --extra <e> \| --with pytest] [--with-requirements <file>...] pytest -q`. It adds `--group`/`--extra` when pytest is only in a non-default group or extra, and `--with pytest` when pytest isn't declared at all. Requirements files are layered on top of the synced project. |
   | No `[project]` (Poetry, tool-only `pyproject.toml`, `setup.py`, requirements files) | `uv run --no-project --with pytest [--with-requirements <file>...] [--with-editable .] [--with <dep>...] python -m pytest -q` |

   For the no-`[project]` row:
   - `--with-editable .` is added when the package can be built: `[build-system]` or `setup.py`; for Poetry, also not `package-mode = false`. The build brings the package's own dependencies.
   - Poetry dependencies go to `--with` with their constraints converted to PEP 440 (`^1.2` → `>=1.2,<2`, `~1.2` → `>=1.2,<1.3`). These are the non-optional groups, plus the main dependencies when the package can't be built.
   - A Poetry dependency that isn't on an index (path, git or url) gives `skipped` with a reason. Only Poetry can install it, and Poetry isn't in the image.
   - `python -m` puts the root on `sys.path`, which flat layouts need.

   Without uv, the command is `python3 -m pytest -q`.
5. `Cargo.toml` → `cargo test`. Rust is not in the image, so this result is `skipped` with a reason.

**Run environment.** The command runs with `CI=true`, so watchers like vitest don't hang. It has no stdin and runs under `timeout` for `AIOS_CHECK_TIMEOUT` seconds (default 600). That budget includes waiting for the per-project lock, which serialises concurrent dependency installs.

**Client gone.** When the reader of the helper's stdout/stderr goes away, the run is stopped: SIGTERM to the process group, then SIGKILL 10 s later. Then the lock is released. This covers Hermes killing its ssh client on `terminal.timeout`. Under ControlMaster, the connection's `sshd-session` survives that kill, and without a pty nothing gets a SIGHUP. But sshd closes the session's pipes, and an `aios_helper.py watch` process beside the command polls them for that. Without this, a slow suite kept running and kept the lock for its whole budget, and the plugin's next `aios-check` got `exit_code 124`.

**Output with `--json`.** Exactly one stdout line, which `tools/hermes-plugin/aios/checks.py run_check` reads as the last line:

```json
{"exit_code": 1, "output_tail": "--- FAIL: TestAdd ...", "command": "go test ./...", "skipped": false,
 "root": "/workspace/tasks/t1", "duration_ms": 812, "timed_out": false, "reason": "only when skipped"}
```

| Field | Meaning |
|---|---|
| `output_tail` | The last `AIOS_CHECK_TAIL_LINES` lines (200), capped at 20 000 characters, with ANSI codes stripped and invalid UTF-8 replaced. The JSON line is ASCII-only. |
| `exit_code` | `124` means the run timed out (`timed_out: true`). |
| `skipped: true` | Nothing was validated, and the plugin then sends `validation: none`. This happens when no test command was found, a runner is missing, pytest exited 5 ("no tests collected"), or a Poetry dependency can't be installed without Poetry. A usage or configuration error also gives `skipped: true`, with exit status 2. |
| `error` | Present only when `aios_helper.py` itself couldn't run. A minimal line is then printed (`exit_code`, `command`, `skipped`, `root`, `timed_out`, empty `output_tail`), so the last line is always JSON. |

The helper always runs on `AIOS_PYTHON` (`/usr/bin/python3`) with `-I`, never on a `python3` from `PATH`.

**Exit status.** The test command's exit status, or 0 when skipped. Without `--json`, output streams live and a PASS/FAIL summary goes to stderr.

**Side effects:**
- `.venv/`, `node_modules/`, `__pycache__/`, `.pytest_cache/`, `.ruff_cache/` and `.mypy_cache/` are added to the repo's `.git/info/exclude`. That file is local and never in a patch.
- Lockfiles the check itself creates (`uv.lock`, `package-lock.json`, `pnpm-lock.yaml`, `yarn.lock`, `bun.lock`) are deleted again when they didn't exist. This covers every directory from the checked project up to the git toplevel, because uv, npm and pnpm workspaces write the lockfile at the workspace root.

### Task lifecycle (§21)

| Command | Behavior |
|---|---|
| `aios-task-start <id> <repo_url> [ref]` | Clones into `$WORKSPACE_ROOT/tasks/<id>` on a new local branch `aios/<id>` and prints the directory. |
| `aios-task-check <id> [cmd...]` | With no command, runs the detected tests (`aios-check --json` on the task). With one argument, runs it as a shell command line. With several, runs them as an argv. Prints the same JSON as `aios-check --json`, and its exit status is the command's. Its budget is `AIOS_TASK_CHECK_TIMEOUT` (280 s), below Hermes' `terminal.timeout` (300 s), so a slow suite returns `exit_code 124` as JSON instead of a killed client. |
| `aios-task-patch <id>` | Writes `$WORKSPACE_ROOT/patches/<id>.patch` and prints the path. |
| `aios-task-destroy <id>` | Removes the task. The patch is kept. |

`aios-task-start`:
- **Task id:** must match `^[A-Za-z0-9_-]{1,64}$`.
- **Repository URL:** only `https://host[:port]/path` or `git@host:path`. URLs with embedded credentials are refused, because they would be stored in `.git/config`.
- **ref:** optional; a branch, tag or commit, fetched if the clone lacks it.
- **Idempotent** for the same id and URL. It records the starting commit as `git config aios.base`. For an empty repository, that is the empty tree, so every commit the agent makes is in the patch.
- **Clone timeout:** `AIOS_CLONE_TIMEOUT` (900 s). The clone is staged next to the destination and renamed, so a failure leaves nothing behind.

`aios-task-patch`:
- Includes everything since `aios.base`: commits on `aios/<id>`, staged, unstaged and untracked files (respecting ignores). A task without `aios.base` (an empty repository started before it was always recorded) is diffed against the empty tree, never against `HEAD`.
- Binary-safe, and always uses `a/` and `b/` prefixes. Apply it with `git apply`.
- Untracked files are added through a throwaway index, so the task's own index is untouched.

`aios-task-destroy`:
- Idempotent.
- Also handles read-only directories and leftovers from an interrupted start.

The `start`, `patch` and `destroy` commands take a per-task lock (`$WORKSPACE_ROOT/.locks`).

### Others

| Command | Behavior |
|---|---|
| `aios-git-credential` | The git credential helper for `https://github.com` only, configured in `/etc/gitconfig`. It reads `$GITHUB_TOKEN` first, then the tmpfs file `/run/aios/github_token`. |
| `aios-healthcheck [port]` | Checks `/proc/net/tcp{,6}` for a listener on :22, without connecting, so it adds no sshd log noise. |

## Security

| Control | Why / how verified |
|---|---|
| `cap_drop: ALL`, `cap_add: CHOWN, SETUID, SETGID, SYS_CHROOT, AUDIT_WRITE, KILL` | **SETUID/SETGID:** the privsep child demotes to `sshd`, and the session to `agent`.<br>**SYS_CHROOT:** the pre-auth child chroots to `/run/sshd`.<br>**AUDIT_WRITE:** Debian builds with `--with-audit=linux`; `sshd-session` links libaudit, and `audit-linux.c` makes an EPERM as root fatal (`linux_audit_write_entry failed`).<br>**CHOWN:** `pty_setowner` for interactive logins (devpts already gives mode 0620, so no FOWNER), plus the entrypoint's chown of `/workspace` and the token file.<br>**KILL:** the root monitor signals its children: `kill(pmonitor->m_pid, SIGKILL)` on the pre-auth `sshd` uid, and `monitor_child_handler` forwards signals to the `agent` uid. |
| No DAC_OVERRIDE, FOWNER or NET_BIND_SERVICE | Every file root touches is root-owned. The authorized keys file is root-owned (`/etc/ssh/authorized_keys/agent`), and the token file is chmod'ed before it is chown'ed. Port 22 binds without NET_BIND_SERVICE because Docker ≥ 20.10 sets `net.ipv4.ip_unprivileged_port_start=0` (moby PR #41030); compose.yaml sets it explicitly. |
| `no-new-privileges:true` | Compatible with privsep. It only blocks gaining privileges through `execve` (setuid bits, file caps), and sshd only *drops* privileges with `setuid()`. `sshd-auth` sets `PR_SET_NO_NEW_PRIVS` itself for its seccomp sandbox. As a side effect, `agent` can never regain capabilities. |
| `UsePAM no` | `pam_loginuid` needs CAP_AUDIT_CONTROL. The account's password field is `*`, because with PAM off sshd rejects `!` (locked) accounts even for key logins. |
| Key-only, `AllowUsers agent`, `PermitRootLogin no`, `AuthenticationMethods publickey` | Keys are copied from the read-only mount into a root-owned file the agent can't modify. Each key is validated, and option-less keys get `restrict,pty`. |
| `DisableForwarding yes` (plus explicit agent/X11/TCP/stream/tunnel `no`), `PermitUserRC no`, `PermitUserEnvironment no` | The tests confirm TCP forwarding is refused with "administratively prohibited". |
| `AcceptEnv GITHUB_TOKEN EDGE_API_KEY HERMES_TENANT` only | Allows Hermes' SendEnv (`terminal.env_passthrough`, a skill's `required_environment_variables`) for the git token and for the tenant-scoped edge key (plus the task tenant) that `skills/knowledge/transcript_to_notes` needs. Any other client env var is dropped (tested). |
| Session env via one generated `SetEnv` line | Container env never reaches SSH sessions. `AIOS_CHECK_TIMEOUT`, `AIOS_TASK_CHECK_TIMEOUT`, `WORKSPACE_ROOT`, `PATH`, `LANG=C.UTF-8`, `GIT_TERMINAL_PROMPT=0` and the corepack settings come from `/etc/ssh/sshd_config.d/00-aios-env.conf`. sshd keeps only the first `SetEnv` it reads, and that line overrides the default PATH. |
| `PATH`: system directories first | `/usr/local/bin:/usr/bin:/bin:/usr/local/go/bin`, then the agent-writable `~/go/bin:~/.local/bin`. This order is in the SetEnv line, in `/etc/profile.d/aios.sh` (appends) and in the agent's `~/.profile`. That `~/.profile` replaces Debian's skel copy, which *prepends* `~/.local/bin` for login shells such as Hermes' `bash -l` snapshot. So `go install`, `uv tool install` or a cloned repo's scripts can't shadow `aios-check`, `git`, `ssh` or `python3`. `/home/agent` is not reset by `aios-task-destroy`. |
| `GITHUB_TOKEN` | Optional. The entrypoint writes it only to the tmpfs `/run/aios/github_token` (0400, agent) and refuses if `/run/aios` is not a tmpfs. It is unset before sshd starts and is never put in session envs, where `env` output would leak it into transcripts. `memswap_limit = mem_limit`, so it can't be swapped. Use a **fine-grained, repo-scoped** token: the agent can read it. |
| Host key in the named volume `sandbox_hostkeys` | The fingerprint is logged at start. Hermes uses `StrictHostKeyChecking=accept-new`, so a stable key keeps `known_hosts` valid across rebuilds. The image ships no host keys. |
| Limits | 2 GB RAM with no swap, 512 pids and 2 CPUs. |
| Network | Only `sandbox_net` (`172.30.1.0/24`), a separate bridge shared with hermes (the SSH client) and edge (bearer key). It is not `internal`, so egress for git clone, PyPI and npm works, and no ports are published. It is deliberately not on `aios`, for two reasons. First, ufw lets `172.30.0.0/24` reach the host's Ollama on `:11434`, which has **no authentication** (`/api/delete` and `/api/pull` would let repository code remove the models or fill the disk). Second, whisper-server (media profile) has no auth either. Docker isolates bridge networks from each other, and the sandbox subnet is outside the ufw rule, so neither is reachable. Every service the sandbox can reach needs its own credential. This holds only while the ufw rule stays `172.30.0.0/24`: a wider rule such as `172.16.0.0/12` would expose Ollama again. |
| Logs | The entrypoint logs JSON. sshd's `-e` lines are wrapped by `jq` into `{ts, level, service, component, msg}`. |

The image runs as root because sshd must, but PID 1 is `tini`. Everything the agent runs is uid 1000 without capabilities.

**Limit of gate evidence.** The agent owns `/home/agent`. Debian's bash sources `~/.bashrc` even for the non-interactive commands sshd runs (`SSH_SOURCE_BASHRC`), so a `~/.bashrc` the agent writes runs before every command, including the plugin's `aios-check`. It can define functions (bash allows `/usr/local/bin/aios-check` as a function name), aliases or a `PATH`. The tests are code the agent edits anyway, so `aios-check` evidence protects against mistakes and stray installs, not against a compromised agent home. A gate that must hold against that needs a separate account or `docker exec`.

## Integration

Wired into the rest of the system (verified by the tests and an end-to-end run against a real sshd):

1. **compose:** `compose.yaml` has the `sandbox` service (profile `agent`), the `sandbox_net` network and the `sandbox_hostkeys` volume. Only `hermes`, `edge` and `bench` join `sandbox_net`; a test fails if anything else does.
2. **hermes:** on `[aios, sandbox_net]`, mounts `./data/sandbox/keys/id_ed25519:/opt/aios/ssh/id_ed25519:ro` (600, uid 1000 = `HERMES_UID`), sets `TERMINAL_SSH_{HOST,USER,PORT,KEY}` and waits for `sandbox: service_healthy`. `make up` runs `make sandbox-keys` first; run `make sandbox-ssh-check` once Hermes is up.
3. **edge:** on `[aios, sandbox_net]`, so `skills/knowledge/transcript_to_notes` reaches `http://edge:8080` from here. `AIOS_EDGE_URL` (Railway: edge-gw) goes into every session through `write_session_env`.
4. **Edge key:** sshd accepts `EDGE_API_KEY` and `HERMES_TENANT`. In a Kanban worker the aios plugin sets `EDGE_API_KEY` to the task tenant's key (`EDGE_TENANT_KEYS`), so a session can only reach its own tenant in edge.
5. **aios plugin:** `run_check` calls `/usr/local/bin/aios-check` by absolute path (`AIOS_CHECK_BIN` overrides it in tests), and `TEST_COMMAND` recognises `aios-task-check`.
6. **Hermes' SSH backend file sync:** it syncs `~/.hermes/{skills,cache}` and any registered credential files into `/home/agent/.hermes`. Don't register secrets the agent shouldn't see.
7. **Timeouts:**
   - Keep the sandbox's `AIOS_CHECK_TIMEOUT` (`SANDBOX_CHECK_TIMEOUT`, 570) below Hermes' `AIOS_CHECK_TIMEOUT` (600). That is the plugin's ssh timeout.
   - Keep `AIOS_TASK_CHECK_TIMEOUT` (`SANDBOX_TASK_CHECK_TIMEOUT`, 280) below `terminal.timeout` (300).
   - Either way, a slow suite comes back as `exit_code 124` from the sandbox. If a client is killed anyway, the run stops (see "Client gone").

## ENV

| Var | Where | Secret | Default | |
|---|---|---|---|---|
| `SANDBOX_CHECK_TIMEOUT` | host `.env` → container `AIOS_CHECK_TIMEOUT` | no | `570` | the plugin's `aios-check` budget in seconds; keep it below Hermes' 600 |
| `SANDBOX_TASK_CHECK_TIMEOUT` | host `.env` → container `AIOS_TASK_CHECK_TIMEOUT` | no | `280` | `aios-task-check` budget; keep it below `terminal.timeout` (300) |
| `SANDBOX_CLONE_TIMEOUT` | host `.env` → container `AIOS_CLONE_TIMEOUT` | no | `900` | |
| `SANDBOX_GITHUB_TOKEN` | host `.env` → container `GITHUB_TOKEN` | **yes** | empty | fine-grained, repo-scoped GitHub token; empty means anonymous HTTPS |
| `AIOS_CHECK_TIMEOUT` | sandbox sessions (via SetEnv) | no | `600` in the helpers | |
| `AIOS_TASK_CHECK_TIMEOUT` | sandbox sessions (via SetEnv) | no | `280` in `aios-task-check` | |
| `AIOS_PYTHON` | helpers, tests | no | `/usr/bin/python3` | interpreter for `aios_helper.py` (needs `tomllib`, 3.11+) |
| `AIOS_CHECK_TAIL_LINES` | sandbox sessions | no | `200` | |
| `AIOS_CLONE_TIMEOUT` | sandbox sessions | no | `900` | |
| `WORKSPACE_ROOT` | sandbox sessions, tests | no | `/workspace` | absolute, never `/` |
| `GITHUB_TOKEN` | container env (entrypoint) or SendEnv | **yes** | — | read by `aios-git-credential` |
| `EDGE_API_KEY` | SendEnv from Hermes (skills component) | **yes** | — | accepted by sshd for `edge_pipeline.sh`; defined by the skills/edge components |
| `AIOS_GITHUB_TOKEN_FILE` | credential helper | no | `/run/aios/github_token` | |
| `AIOS_AUTHORIZED_KEYS` | entrypoint | no | `/opt/aios/ssh/authorized_keys` | |
| `TERMINAL_SSH_HOST/USER/PORT/KEY` | hermes (existing Hermes names) | no | `sandbox` / `agent` / `22` / `/opt/aios/ssh/id_ed25519` | |
| `SANDBOX_KEYS_DIR`, `SANDBOX_WORKSPACE_DIR` | make variables | no | `data/sandbox/keys`, `data/sandbox/workspace` | |
| `SANDBOX_SSHD_BIN`, `SANDBOX_SSHD_LIBS`, `JQ_BIN` | tests only | no | — | enable the optional sshd/jq test sections |

## Make targets (`mk/sandbox.mk`)

| Target | |
|---|---|
| `sandbox-keys` | Creates `data/sandbox/keys/id_ed25519` (600) and `.pub` (644) and ensures the public key is in `authorized_keys` (644). Also creates `data/sandbox/workspace`. Idempotent. |
| `sandbox-build` | `docker compose --profile agent build sandbox` |
| `sandbox-up` | `sandbox-keys`, then `up -d --wait sandbox` |
| `sandbox-shell` | Shell as `agent` in `/workspace` (`docker exec`, bypassing sshd, with `GITHUB_TOKEN` removed from the env) |
| `sandbox-ssh-check` | Runs the real path from the hermes container: `ssh agent@sandbox aios-check --json /workspace` |
| `sandbox-test` | `bash workers/sandbox/tests/run.sh` |
| `sandbox-verify-downloads` | Re-downloads every pinned artifact and checks its sha256, the base tag and digest, and the npm/PyPI pins |

## Tests

```bash
cd workers/sandbox && bash tests/run.sh          # no Docker needed
```

The suite runs every helper against throwaway git repos:
- a Go module with passing and failing tests;
- Python via uv: a standard layout with `tests/conftest.py` and `tests/pytest.ini` plus a runtime dependency, a requirements-only app, a tool-only `pyproject.toml`, `requirements-dev.txt`, Poetry (editable install with its dev group, and a git dependency that is skipped) and a uv workspace member (no `uv.lock` left at the root). Also the `python3 -m pytest` fallback with uv hidden;
- npm, including an install from `file:` dependencies;
- a repo with no tests (skipped), a Makefile priority case, a monorepo, a timeout case and output truncation;
- a client that disappears mid-run, in `--json` and human mode: the test must stop, the helper must clean up and the next check must get the lock;
- a broken `python3` first in `PATH` and a broken `AIOS_PYTHON` (fallback JSON line);
- the task lifecycle against a local "remote" mapped by `url.insteadOf`, including patch apply round-trip, URL/id injection attempts, the `aios-task-check` budget and an empty repository;
- the credential helper (also through `git credential fill`), the healthcheck and the make targets.

It also checks the contract against the real `tools/hermes-plugin/aios/checks.py` `run_check`, through a fake `ssh`.

The static checks are:
- `bash -n`;
- shellcheck, via `uvx --from shellcheck-py`;
- the Dockerfile pins;
- the `sandbox` service in compose.yaml: structure, network membership and budgets;
- the PATH order in the entrypoint, `profile.sh` and the agent's `~/.profile`.

Optional sections:
- **`SANDBOX_SSHD_BIN`** runs `sshd -t` and an **end-to-end run over a real unprivileged sshd** with this `sshd_config`. That run covers:
  - the plugin's `run_check` over real ssh;
  - SetEnv and AcceptEnv;
  - scp through internal-sftp;
  - refused forwarding;
  - Hermes' terminal-timeout case: the ssh mux client is SIGKILLed while the ControlMaster connection stays up, and the remote test must stop and free the lock.

  For example, use Debian's `openssh-server` binaries, with `SANDBOX_SSHD_LIBS` pointing at the `libwtmpdb0` library dir.
- **`JQ_BIN`** checks the entrypoint's sshd JSON log filter.

Hermes' own `SSHEnvironment` (v2026.9.24) was also driven against that sshd once, by hand. It exercised the `bash -l` snapshot, ControlMaster, the `~/.hermes` sync, cwd/env persistence and `aios-task-check`.
