# shellcheck shell=bash
# Shared code for the aios-* sandbox helpers: sourced by bin/aios-*, never executed directly.
# Layout: image /usr/local/{bin,lib/aios}; repo workers/sandbox/{bin,lib/aios} (tests run from the repo).

# Command substitutions inherit errexit, so a failing helper inside $(...) cannot yield a half-built path.
shopt -s inherit_errexit

AIOS_PROG=${0##*/}
AIOS_LIB_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
AIOS_ID_RE='^[A-Za-z0-9_-]{1,64}$'
AIOS_CLEANUP=()
AIOS_CHILDREN=() # background pids (a check's `timeout`, its watcher) that must not outlive the helper

aios_log() { printf '%s: %s\n' "$AIOS_PROG" "$*" >&2; }
aios_die() { aios_log "$*"; exit 1; }
aios_usage_die() { aios_log "$*"; exit 2; }

aios_cleanup() {
  local p
  for p in "${AIOS_CHILDREN[@]}"; do
    kill -TERM "$p" 2>/dev/null || true
  done
  for p in "${AIOS_CLEANUP[@]}"; do
    [[ -n $p ]] && rm -rf -- "$p"
  done
  return 0
}
trap aios_cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

# The system interpreter, never a python3 from the agent-writable PATH (e.g. a `uv python install --default`
# shim without tomllib); -I: no cwd/PYTHONPATH on sys.path, so a repo's json.py cannot shadow the stdlib.
AIOS_PYTHON=${AIOS_PYTHON:-/usr/bin/python3}
aios_py() { "$AIOS_PYTHON" -I "$AIOS_LIB_DIR/aios_helper.py" "$@"; }

# aios_int_env NAME DEFAULT: positive integer from the environment, DEFAULT when unset or invalid.
aios_int_env() {
  local name=$1 def=$2 value=${!1:-}
  if [[ -z $value ]]; then
    value=$def
  elif ! [[ $value =~ ^[1-9][0-9]{0,5}$ ]]; then
    aios_log "ignoring invalid $name='$value', using $def"
    value=$def
  fi
  printf '%s\n' "$value"
}

# Absolute, normalised WORKSPACE_ROOT (default /workspace); never "/".
aios_workspace_root() {
  local root=${WORKSPACE_ROOT:-/workspace}
  [[ $root == /* ]] || aios_die "WORKSPACE_ROOT must be an absolute path (got '$root')"
  root=$(realpath -m -- "$root")
  [[ $root != / ]] || aios_die "WORKSPACE_ROOT must not be /"
  printf '%s\n' "$root"
}

aios_require_id() {
  [[ $1 =~ $AIOS_ID_RE ]] || aios_usage_die "invalid task id '$1' (allowed: $AIOS_ID_RE)"
}

aios_task_dir() {
  local root
  root=$(aios_workspace_root) || return 1
  printf '%s/tasks/%s\n' "$root" "$1"
}

# aios_lock FILE WAIT_SECONDS: exclusive lock held until the process exits (fd in AIOS_LOCK_FD).
# Returns 1 when the wait expires. Without flock(1) it degrades to no locking.
aios_lock() {
  local file=$1 wait=$2
  mkdir -p -- "$(dirname -- "$file")"
  exec {AIOS_LOCK_FD}>>"$file"
  command -v flock >/dev/null 2>&1 || return 0
  flock -w "$wait" "$AIOS_LOCK_FD"
}

aios_task_lock() {
  local id=$1 root
  root=$(aios_workspace_root) || exit 1
  aios_lock "$root/.locks/$id.lock" "${2:-60}" || aios_die "task $id is busy (another aios-task-* holds its lock)"
}

# Tool artifacts that checks create inside repos; kept out of patches through .git/info/exclude
# (local to the clone, never part of a diff).
AIOS_ARTIFACT_EXCLUDES=(.venv/ node_modules/ __pycache__/ .pytest_cache/ .ruff_cache/ .mypy_cache/)

aios_exclude_artifacts() {
  local dir=$1 exclude pattern
  exclude=$(git -C "$dir" rev-parse --path-format=absolute --git-path info/exclude 2>/dev/null) || return 0
  mkdir -p -- "$(dirname -- "$exclude")"
  touch -- "$exclude"
  for pattern in "${AIOS_ARTIFACT_EXCLUDES[@]}"; do
    grep -qxF -- "$pattern" "$exclude" || printf '%s\n' "$pattern" >>"$exclude"
  done
}

# Removes a directory tree even when it contains read-only directories (e.g. Go module caches).
aios_rm_tree() {
  local path=$1
  if [[ -L $path ]]; then
    rm -f -- "$path"
    return 0
  fi
  [[ -e $path ]] || return 0
  chmod -R u+rwX -- "$path" 2>/dev/null || true
  rm -rf -- "$path"
}

# Microseconds since the epoch (bash 5 EPOCHREALTIME, locale-independent).
aios_now_us() { printf '%s\n' "${EPOCHREALTIME//[!0-9]/}"; }
