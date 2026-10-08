# shellcheck shell=bash disable=SC2034  # AIOS_CMD/AIOS_KIND/AIOS_ROOT/AIOS_SKIP_REASON are read by the callers
# Test-command detection and the timed run/report used by aios-check and aios-task-check.
# Sourced after common.sh. Result contract (tools/hermes-plugin/aios/checks.py run_check): the LAST stdout
# line in JSON mode is {"exit_code", "output_tail", "command", "skipped", ...}; skipped=true means nothing
# was validated (no test command, runner missing, or pytest collected no tests).

# A reader that went away (SSH client gone) must cost a write error, not the cleanup; the test command
# gets the default disposition back in aios_run_check.
trap '' PIPE

# Lockfiles a check may create as a side effect (uv run, npm/pnpm/bun install), possibly at a workspace
# root above the checked project; removed again when they did not exist, so a check never adds files to
# the task's patch.
AIOS_SIDE_EFFECT_FILES=(uv.lock package-lock.json pnpm-lock.yaml yarn.lock bun.lock)
# Requirements files a Python project's tests may need; each one present is passed to uv --with-requirements.
AIOS_PY_REQUIREMENTS=(requirements.txt requirements-dev.txt requirements_dev.txt dev-requirements.txt
  requirements-test.txt requirements_test.txt requirements-tests.txt test-requirements.txt)

# aios_quote WORD...: one shell command line, single-quoting only the words that need it.
aios_quote() {
  local word sq="'\\''"
  local -a out=()
  for word in "$@"; do
    if [[ $word =~ ^[A-Za-z0-9_./=:,@+%-]+$ ]]; then
      out+=("$word")
    else
      out+=("'${word//\'/$sq}'")
    fi
  done
  printf '%s\n' "${out[*]}"
}

# ----------------------------------------------------------------------------- path resolution

# aios_resolve_dir PATH: existing directory for PATH. Relative paths are tried against $PWD, then
# $WORKSPACE_ROOT, then the most recently modified match under $WORKSPACE_ROOT/tasks/* (Hermes reports
# paths relative to the terminal cwd, which this SSH session does not share). Missing paths (deleted
# files) resolve to their nearest existing ancestor.
aios_resolve_dir() {
  local path=$1 root candidate best="" best_mtime=-1 mtime
  root=$(aios_workspace_root)
  # shellcheck disable=SC2088  # matching a literal "~" on purpose
  case $path in
    "~") path=$HOME ;;
    "~/"*) path=$HOME/${path#"~/"} ;;
  esac
  if [[ $path != /* ]]; then
    if [[ -e $PWD/$path ]]; then
      path=$PWD/$path
    elif [[ -e $root/$path ]]; then
      path=$root/$path
    else
      for candidate in "$root"/tasks/*/"$path"; do
        [[ -e $candidate ]] || continue
        mtime=$(stat -c %Y -- "$candidate" 2>/dev/null || echo 0)
        if ((mtime > best_mtime)); then
          best=$candidate best_mtime=$mtime
        fi
      done
      path=${best:-$PWD/$path}
    fi
  fi
  path=$(realpath -m -- "$path")
  while [[ ! -e $path ]]; do
    path=$(dirname -- "$path")
  done
  [[ -d $path ]] || path=$(dirname -- "$path")
  realpath -e -- "$path"
}

# aios_project_boundary DIR: git toplevel; outside git, $WORKSPACE_ROOT when DIR is under it, else /.
aios_project_boundary() {
  local dir=$1 top root
  if top=$(git -C "$dir" rev-parse --show-toplevel 2>/dev/null) && [[ -n $top ]]; then
    printf '%s\n' "$top"
    return 0
  fi
  root=$(aios_workspace_root)
  case $dir/ in
    "$root"/*) printf '%s\n' "$root" ;;
    *) printf '/\n' ;;
  esac
}

# ----------------------------------------------------------------------------- detection

aios_need() {
  command -v "$1" >/dev/null 2>&1 && return 0
  AIOS_SKIP_REASON="$2 found but '$1' is not installed in the sandbox"
  return 1
}

# aios_has_make_target DIR TARGET: TARGET is a real rule in DIR's makefile (included files count; a
# file or directory merely named TARGET does not). Reads make's database without running recipes.
aios_has_make_target() {
  local dir=$1 target=$2 name found="" db
  for name in GNUmakefile makefile Makefile; do
    if [[ -f $dir/$name ]]; then
      found=$name
      break
    fi
  done
  [[ -n $found ]] || return 1
  command -v make >/dev/null 2>&1 || return 1
  db=$(cd -- "$dir" && timeout 30 make -prq --no-print-directory 2>/dev/null </dev/null) || true
  awk -v t="$target" '
    /^# Not a target:/ { skip = 1; next }
    /^[^#\t%.=][^:=]*:([^=]|$)/ && !skip {
      split($0, parts, ":"); n = split(parts[1], names, " ")
      for (i = 1; i <= n; i++) if (names[i] == t) found = 1
    }
    { skip = 0 }
    END { exit found ? 0 : 1 }' <<<"$db"
}

aios_detect_node() {
  local dir=$1 info key value has_test=1 deps=0 pm="" runner exe install=""
  # Unreadable/invalid package.json: let the package manager report it as the failure.
  info=$(aios_py npm-info "$dir/package.json" 2>/dev/null) || info=""
  while IFS='=' read -r key value; do
    case $key in
      test) has_test=$value ;;
      deps) deps=$value ;;
      pm) pm=$value ;;
    esac
  done <<<"$info"
  [[ $has_test == 1 ]] || return 1

  if [[ $pm == pnpm || (-z $pm && -f $dir/pnpm-lock.yaml) ]]; then
    runner=pnpm
  elif [[ $pm == yarn || (-z $pm && -f $dir/yarn.lock) ]]; then
    runner=yarn
  elif [[ $pm == bun || (-z $pm && (-f $dir/bun.lock || -f $dir/bun.lockb)) ]]; then
    runner=bun
  else
    runner=npm
  fi
  exe=$runner
  if ! command -v "$runner" >/dev/null 2>&1; then
    if [[ $runner == pnpm || $runner == yarn ]] && command -v corepack >/dev/null 2>&1; then
      exe="corepack $runner"
    else
      AIOS_SKIP_REASON="package.json uses $runner, which is not installed in the sandbox"
      return 2
    fi
  fi

  if [[ $deps == 1 && ! -d $dir/node_modules ]]; then
    case $runner in
      pnpm)
        install="$exe install"
        if [[ -f $dir/pnpm-lock.yaml ]]; then install+=" --frozen-lockfile"; fi
        ;;
      yarn) install="$exe install" ;;
      bun) install="bun install" ;;
      npm)
        if [[ -f $dir/package-lock.json || -f $dir/npm-shrinkwrap.json ]]; then
          install="npm ci --no-audit --no-fund"
        else
          install="npm install --no-audit --no-fund"
        fi
        ;;
    esac
  fi
  AIOS_CMD="${install:+$install && }$exe test"
  AIOS_KIND=node
  return 0
}

# aios_python_marker DIR: 0 = installable Python project root (setup.py, or a pyproject.toml with [project],
# [tool.poetry] or [build-system], or one that does not parse, which uv then reports); 3 = test config or
# requirements only (tool-only pyproject.toml, setup.cfg, pytest.ini, tox.ini, requirements files); 1 = none.
# conftest.py is no marker at all: tests/conftest.py does not make tests/ a project. Sets AIOS_PYINFO
# (aios_helper.py py-info of DIR/pyproject.toml).
aios_python_marker() {
  local dir=$1 name
  AIOS_PYINFO=""
  if [[ -f $dir/pyproject.toml ]]; then
    AIOS_PYINFO=$(aios_py py-info "$dir/pyproject.toml" 2>/dev/null) || AIOS_PYINFO=$'table=invalid\nbuild=0'
    [[ $AIOS_PYINFO == $'table=none\nbuild=0'* ]] || return 0
  fi
  [[ ! -f $dir/setup.py ]] || return 0
  [[ -z $AIOS_PYINFO ]] || return 3
  for name in setup.cfg pytest.ini tox.ini "${AIOS_PY_REQUIREMENTS[@]}"; do
    [[ ! -f $dir/$name ]] || return 3
  done
  return 1
}

# aios_detect_python DIR: pytest command for DIR, after aios_python_marker DIR.
aios_detect_python() {
  local dir=$1 key value table=none build=0 mode=dev installable=0 unsupported="" name
  local -a withs=() reqs=() install=()
  AIOS_KIND=pytest
  while IFS='=' read -r key value; do
    case $key in
      table) table=$value ;;
      build) build=$value ;;
      mode) mode=$value ;;
      installable) installable=$value ;;
      with) withs+=(--with "$value") ;;
      unsupported) unsupported=${unsupported:-$value} ;;
    esac
  done <<<"$AIOS_PYINFO"

  if ! command -v uv >/dev/null 2>&1; then
    if command -v python3 >/dev/null 2>&1 && python3 -I -c 'import pytest' >/dev/null 2>&1; then
      AIOS_CMD="python3 -m pytest -q"
      return 0
    fi
    AIOS_SKIP_REASON="Python project found but neither uv nor pytest is installed in the sandbox"
    return 2
  fi
  for name in "${AIOS_PY_REQUIREMENTS[@]}"; do
    [[ ! -f $dir/$name ]] || reqs+=(--with-requirements "$name")
  done

  case $table in
    project) # uv syncs the project and its dependencies; requirements files are layered on top
      case $mode in
        group:*) install=(--group "${mode#group:}") ;;
        extra:*) install=(--extra "${mode#extra:}") ;;
        none) install=(--with pytest) ;;
      esac
      AIOS_CMD="$(aios_quote uv run "${install[@]}" "${reqs[@]}") pytest -q"
      return 0
      ;;
    invalid)
      AIOS_CMD="uv run pytest -q" # uv reports what is wrong with pyproject.toml
      return 0
      ;;
    poetry)
      if [[ -n $unsupported ]]; then
        AIOS_SKIP_REASON="Poetry dependency '$unsupported' is not on a package index (path/git/url); only poetry, which is not in the sandbox, can install it"
        return 2
      fi
      if ((installable)); then install=(--with-editable .); fi
      install+=("${withs[@]}")
      ;;
    *)
      if ((build)) || [[ -f $dir/setup.py ]]; then install=(--with-editable .); fi
      ;;
  esac
  # No PEP 621 project for uv to sync: an ephemeral environment with pytest, the requirements files and the
  # package itself when it can be built; `python -m` also puts the root on sys.path (flat layouts).
  AIOS_CMD="uv run $(aios_quote --no-project --with pytest "${reqs[@]}" "${install[@]}") python -m pytest -q"
  return 0
}

# aios_detect DIR: sets AIOS_CMD/AIOS_KIND (and AIOS_PY_WEAK=1 for a pytest command found through weak
# markers only). Returns 0 when DIR has a runnable test command, 1 when DIR has no recognised manifest,
# 2 when it has one but its runner is missing (AIOS_SKIP_REASON).
# Priority: Makefile test target > go.mod > package.json test script > pytest > Cargo.toml.
aios_detect() {
  local dir=$1 rc=0 py=0
  AIOS_CMD="" AIOS_KIND="" AIOS_SKIP_REASON="" AIOS_PY_WEAK=0
  if aios_has_make_target "$dir" test; then
    AIOS_CMD="make test" AIOS_KIND=make
    return 0
  fi
  if [[ -f $dir/go.mod ]]; then
    aios_need go go.mod || return 2
    AIOS_CMD="go test ./..." AIOS_KIND=go
    return 0
  fi
  if [[ -f $dir/package.json ]]; then
    aios_detect_node "$dir" || rc=$?
    ((rc == 1)) || return "$rc"
    rc=0
  fi
  aios_python_marker "$dir" || py=$?
  if ((py != 1)); then
    if ((py == 3)); then AIOS_PY_WEAK=1; fi
    aios_detect_python "$dir" || rc=$?
    return "$rc"
  fi
  if [[ -f $dir/Cargo.toml ]]; then
    aios_need cargo Cargo.toml || return 2
    AIOS_CMD="cargo test" AIOS_KIND=cargo
    return 0
  fi
  return 1
}

# aios_python_root_above DIR BOUNDARY: prints the nearest ancestor of DIR, up to BOUNDARY, that is an
# installable Python project root (aios_python_marker 0).
aios_python_root_above() {
  local dir=$1 boundary=$2 rc
  while [[ $dir != "$boundary" && $dir != / ]]; do
    dir=$(dirname -- "$dir")
    rc=0
    aios_python_marker "$dir" || rc=$?
    if ((rc == 0)); then
      printf '%s\n' "$dir"
      return 0
    fi
  done
  return 1
}

# aios_find_project START BOUNDARY: nearest directory from START up to BOUNDARY with a test command. A
# pytest directory with weak markers only (tests/ with pytest.ini, a requirements.txt app) defers to the
# nearest installable Python root above it, so the project and its dependencies get installed.
# Sets AIOS_ROOT; return codes as aios_detect (1 leaves AIOS_ROOT=BOUNDARY).
aios_find_project() {
  local dir=$1 boundary=$2 rc up
  while :; do
    rc=0
    aios_detect "$dir" || rc=$?
    if ((rc != 1)); then
      AIOS_ROOT=$dir
      if ((AIOS_PY_WEAK)) && up=$(aios_python_root_above "$dir" "$boundary"); then
        rc=0
        aios_detect "$up" || rc=$?
        AIOS_ROOT=$up
      fi
      return "$rc"
    fi
    [[ $dir != "$boundary" && $dir != / ]] || break
    dir=$(dirname -- "$dir")
  done
  AIOS_ROOT=$boundary
  return 1
}

# ----------------------------------------------------------------------------- run + report

# aios_json_string S: S as an ASCII-only JSON string literal (other bytes become '?').
aios_json_string() {
  local LC_ALL=C s=$1
  s=${s//\\/\\\\}
  s=${s//\"/\\\"}
  s=${s//[^ -~]/?}
  printf '"%s"' "$s"
}

# aios_result ARGS...: `aios_helper.py result ARGS...` (output tail on stdin). When the helper cannot run, a
# minimal line built here keeps the contract that the last stdout line is the JSON result.
aios_result() {
  local arg exit_code=1 command="" root="" reason="" skipped=false timed_out=false line
  aios_py result "$@" && return 0
  for arg in "$@"; do
    case $arg in
      --exit-code=*) exit_code=${arg#*=} ;;
      --command=*) command=${arg#*=} ;;
      --root=*) root=${arg#*=} ;;
      --reason=*) reason=${arg#*=} ;;
      --skipped) skipped=true ;;
      --timed-out) timed_out=true ;;
    esac
  done
  [[ $exit_code =~ ^-?[0-9]{1,6}$ ]] || exit_code=1
  line="{\"exit_code\": $exit_code, \"output_tail\": \"\", \"command\": $(aios_json_string "$command")"
  line+=", \"skipped\": $skipped, \"root\": $(aios_json_string "$root"), \"timed_out\": $timed_out"
  if [[ -n $reason ]]; then line+=", \"reason\": $(aios_json_string "$reason")"; fi
  line+=", \"error\": $(aios_json_string "result helper failed ($AIOS_PYTHON); output_tail not captured")}"
  printf '%s\n' "$line"
}

# aios_emit_skip JSON ROOT REASON
aios_emit_skip() {
  local json=$1 root=$2 reason=$3
  if ((json)); then
    aios_result --exit-code=0 --skipped --reason="$reason" --root="$root" </dev/null
  else
    aios_log "skipped: $reason (${root})"
  fi
}

# aios_new_side_effects ROOT: lockfile paths (AIOS_SIDE_EFFECT_FILES) that do not exist yet in ROOT or any
# directory above it up to its project boundary: uv, npm and pnpm workspaces write the lockfile at the
# workspace root, not in the member being checked.
aios_new_side_effects() {
  local dir=$1 boundary name
  boundary=$(aios_project_boundary "$dir")
  while :; do
    for name in "${AIOS_SIDE_EFFECT_FILES[@]}"; do
      [[ -e $dir/$name || -L $dir/$name ]] || printf '%s\n' "$dir/$name"
    done
    [[ $dir != "$boundary" && $dir != / ]] || break
    dir=$(dirname -- "$dir")
  done
}

# aios_run_check ROOT COMMAND JSON KIND: runs COMMAND with bash in ROOT under AIOS_CHECK_TIMEOUT (shared with
# the wait for the per-project lock) and reports it. JSON=1: output only in output_tail, report as the last
# stdout line; JSON=0: live output plus a one-line summary on stderr. Returns the command's exit code
# (0 when the result is skipped).
#
# The command runs as a background job of this shell, in `timeout`'s own process group, beside an
# `aios_helper.py watch` that stops the group (SIGTERM, SIGKILL 10 s later) as soon as this helper's
# stdout/stderr lose their reader: when Hermes kills its ssh client on a terminal timeout, sshd closes the
# session's pipes but signals nothing, and the check would otherwise keep running, and keep the lock,
# for the rest of its budget.
aios_run_check() {
  local root=$1 cmd=$2 json=$3 kind=${4:-custom}
  local budget lines tmp log out lock rc=0 remaining t0 elapsed_us timed_out=0 skipped=0 reason="" name
  local pid watcher tee_pid=""
  local -a created=() flags=() run_env=(CI=true GIT_TERMINAL_PROMPT=0)
  budget=$(aios_int_env AIOS_CHECK_TIMEOUT 600)
  lines=$(aios_int_env AIOS_CHECK_TAIL_LINES 200)
  tmp=$(mktemp -d "${TMPDIR:-/tmp}/aios-check.XXXXXX")
  AIOS_CLEANUP+=("$tmp")
  log=$tmp/output.log
  : >"$log"
  t0=$(aios_now_us)

  # Dependency installs (pnpm/npm/uv) are not safe to run concurrently in one tree: serialise per root.
  lock=${TMPDIR:-/tmp}/aios-check-$(printf '%s' "$root" | sha256sum | cut -c1-16).lock
  if ! aios_lock "$lock" "$budget"; then
    rc=124 timed_out=1
    printf 'aios-check: another check of %s held its lock for %ss\n' "$root" "$budget" >>"$log"
  else
    remaining=$((budget - ($(aios_now_us) - t0) / 1000000))
    ((remaining > 0)) || remaining=1
    mapfile -t created < <(aios_new_side_effects "$root")

    set +o errexit
    out=$log
    if ((json)); then
      run_env+=(NO_COLOR=1 FORCE_COLOR=0 TERM=dumb)
    else
      out=$tmp/live # live output: tee reads a FIFO, so the command itself stays a job of this shell
      mkfifo -- "$out"
      (
        trap - PIPE
        exec tee -- "$log"
      ) <"$out" {AIOS_LOCK_FD}>&- &
      tee_pid=$!
    fi
    (
      trap - PIPE
      cd -- "$root" && exec env "${run_env[@]}" timeout --kill-after=10s "${remaining}s" bash -c "$cmd"
    ) </dev/null >"$out" 2>&1 {AIOS_LOCK_FD}>&- &
    pid=$!
    (exec "$AIOS_PYTHON" -I "$AIOS_LIB_DIR/aios_helper.py" watch "--pid=$pid" "--parent=$$") </dev/null {AIOS_LOCK_FD}>&- &
    watcher=$!
    AIOS_CHILDREN+=("$pid" "$watcher")
    wait "$pid"
    rc=$?
    kill "$watcher" 2>/dev/null
    wait "$watcher" 2>/dev/null
    if [[ -n $tee_pid ]]; then wait "$tee_pid"; fi
    AIOS_CHILDREN=()
    set -o errexit

    elapsed_us=$(($(aios_now_us) - t0))
    if ((rc == 124 || (rc == 137 && elapsed_us / 1000000 >= remaining))); then
      timed_out=1
      printf '\naios-check: timed out after %ss\n' "$remaining" >>"$log"
    fi
    if [[ $kind == pytest && $rc == 5 ]]; then
      skipped=1 reason="pytest collected no tests"
    fi
    for name in "${created[@]}"; do
      [[ ! -f $name ]] || rm -f -- "$name"
    done
  fi

  elapsed_us=$(($(aios_now_us) - t0))
  if ((json)); then
    if ((skipped)); then flags+=(--skipped "--reason=$reason"); fi
    if ((timed_out)); then flags+=(--timed-out); fi
    { tail -n "$lines" -- "$log" || true; } | aios_result "--exit-code=$rc" "--command=$cmd" "--root=$root" \
      "--duration-ms=$((elapsed_us / 1000))" "${flags[@]}" || true
  elif ((skipped)); then
    aios_log "skipped: $reason ($cmd in $root)"
  else
    aios_log "$( ((rc == 0)) && echo PASS || echo FAIL) exit=$rc$( ((timed_out)) && echo ' (timeout)') — $cmd in $root ($((elapsed_us / 1000000))s)"
  fi
  ((skipped)) && return 0
  return "$rc"
}
