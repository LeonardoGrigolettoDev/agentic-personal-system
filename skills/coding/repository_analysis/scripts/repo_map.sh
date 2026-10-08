#!/usr/bin/env bash
# repo_map.sh — deterministic map of a repository: languages, manifests, build/test/lint commands,
# entrypoints, top-level tree, recent git history and open-marker (TO-DO, FIX-ME) counts. Zero tokens, read-only.
#
#   repo_map.sh [--format json|md] [--commits N] [--max-entries N] [PATH]
#
# Requires bash + POSIX tools (awk, sort, git when PATH is a git work tree). No jq needed.
# Exit codes: 0 ok, 2 usage error / path not found.
set -eu  # no pipefail: grep without matches and head closing pipes are expected here

FORMAT=json
COMMITS=10
MAX_ENTRIES=60
ROOT=.

usage() { sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --format) FORMAT=${2:-}; shift 2 ;;
    --format=*) FORMAT=${1#*=}; shift ;;
    --commits) COMMITS=${2:-}; shift 2 ;;
    --max-entries) MAX_ENTRIES=${2:-}; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    -*) echo "repo_map: opção desconhecida: $1" >&2; usage >&2; exit 2 ;;
    *) ROOT=$1; shift ;;
  esac
done
case "$FORMAT" in json|md|markdown) ;; *) echo "repo_map: --format deve ser json ou md" >&2; exit 2 ;; esac
case "$COMMITS$MAX_ENTRIES" in *[!0-9]*|"") echo "repo_map: --commits/--max-entries devem ser inteiros" >&2; exit 2 ;; esac
[ -d "$ROOT" ] || { echo "repo_map: diretório não encontrado: $ROOT" >&2; exit 2; }
cd "$ROOT"
ROOT_ABS=$(pwd -P)

TMP=$(mktemp -d "${TMPDIR:-/tmp}/repo_map.XXXXXX")
trap 'rm -rf "$TMP"' EXIT
FILES=$TMP/files

PRUNE_DIRS='.git node_modules vendor .venv venv __pycache__ dist build target .next .nuxt .cache .tox .mypy_cache .pytest_cache .ruff_cache coverage'

IS_GIT=false
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  IS_GIT=true
  git -c core.quotePath=false ls-files --cached --others --exclude-standard 2>/dev/null | sort -u > "$FILES"
else
  prune_expr=""
  for d in $PRUNE_DIRS; do prune_expr="$prune_expr -name $d -o"; done
  # shellcheck disable=SC2086
  find . -type d \( ${prune_expr% -o} \) -prune -o -type f -print 2>/dev/null | sed 's|^\./||' | sort > "$FILES"
fi
FILES_TOTAL=$(wc -l < "$FILES" | tr -d ' ')

# ------------------------------------------------------------------ languages (file count by extension)
awk -F/ '
  BEGIN {
    split("go:Go py:Python ipynb:Jupyter ts:TypeScript tsx:TypeScript js:JavaScript jsx:JavaScript mjs:JavaScript cjs:JavaScript rs:Rust java:Java kt:Kotlin rb:Ruby php:PHP cs:C# c:C h:C cpp:C++ cc:C++ cxx:C++ hpp:C++ swift:Swift scala:Scala sql:SQL sh:Shell bash:Shell zsh:Shell html:HTML css:CSS scss:CSS vue:Vue svelte:Svelte md:Markdown yaml:YAML yml:YAML json:JSON toml:TOML dart:Dart ex:Elixir exs:Elixir lua:Lua r:R proto:Protobuf tf:Terraform", pairs, " ")
    for (i in pairs) { split(pairs[i], kv, ":"); lang[kv[1]] = kv[2] }
  }
  {
    name = $NF; ext = ""
    if (name ~ /^Dockerfile/) { count["Dockerfile"]++; next }
    if (match(name, /\.[^.]+$/)) ext = tolower(substr(name, RSTART + 1))
    if (ext in lang) count[lang[ext]]++
  }
  END { for (l in count) printf "%d\t%s\n", count[l], l }
' "$FILES" | sort -k1,1nr -k2 | head -n 12 > "$TMP/languages"

# ------------------------------------------------------------------ manifests (root + nested, depth <= 3)
awk -F/ 'NF <= 3 && $NF ~ /^(go\.mod|go\.work|package\.json|pnpm-workspace\.yaml|pyproject\.toml|setup\.py|requirements[^\/]*\.txt|uv\.lock|poetry\.lock|Cargo\.toml|pom\.xml|build\.gradle(\.kts)?|Gemfile|composer\.json|Makefile|GNUmakefile|justfile|Taskfile\.ya?ml|Dockerfile[^\/]*|(docker-)?compose[^\/]*\.ya?ml|tsconfig\.json|deno\.jsonc?)$/' "$FILES" \
  | head -n 40 > "$TMP/manifests"
ls .github/workflows/*.y*ml >/dev/null 2>&1 && ls -1 .github/workflows/*.y*ml >> "$TMP/manifests"

# ------------------------------------------------------------------ build / test / lint commands
: > "$TMP/commands"
cmd() { printf '%s\t%s\n' "$1" "$2" >> "$TMP/commands"; }
has() { [ -f "$1" ]; }

if has go.mod; then
  cmd build "go build ./..."; cmd test "go test ./..."; cmd lint "go vet ./..."
  if has .golangci.yml || has .golangci.yaml; then cmd lint "golangci-lint run"; fi
fi
if has package.json; then
  pm=npm
  if has pnpm-lock.yaml; then pm=pnpm; elif has yarn.lock; then pm=yarn; elif has bun.lockb || has bun.lock; then pm=bun; fi
  scripts=$(grep -oE '"(test|test:unit|build|lint|typecheck|type-check|check|e2e)"[[:space:]]*:' package.json 2>/dev/null \
    | sed -E 's/"([^"]+)".*/\1/' | sort -u || true)
  for s in $scripts; do
    case "$s" in
      test|test:unit|e2e) cmd test "$pm run $s" ;;
      build) cmd build "$pm run build" ;;
      *) cmd lint "$pm run $s" ;;
    esac
  done
  if has tsconfig.json && ! printf '%s\n' "$scripts" | grep -qE '^(typecheck|type-check)$'; then cmd lint "$pm exec tsc --noEmit"; fi
  if ! printf '%s\n' "$scripts" | grep -qx lint && ls eslint.config.* .eslintrc* >/dev/null 2>&1; then cmd lint "$pm exec eslint ."; fi
fi
# Python: commands that run in the sandbox (python3 + uv, ruff on PATH; no bare `python`, no global
# pytest/mypy), mirroring how aios-check picks the test command.
if has pyproject.toml || has setup.py || ls requirements*.txt >/dev/null 2>&1; then
  if has poetry.lock && ! grep -qs '^\[project\]' pyproject.toml; then
    run="poetry run"                                   # Poetry-only metadata: uv cannot run it
  elif has pyproject.toml && grep -qs '^\[project\]' pyproject.toml; then
    run="uv run"                                       # PEP 621 project: uv builds its environment
  else
    run="uv run --no-project"                          # setup.py / requirements.txt only
    has requirements.txt && run="$run --with-requirements requirements.txt"
  fi
  if has pytest.ini || has conftest.py || [ -d tests ] || [ -d test ] || grep -qs -e pytest pyproject.toml requirements*.txt setup.cfg; then
    if [ "$run" = "uv run" ] && grep -qs pytest pyproject.toml; then cmd test "uv run pytest -q"
    elif [ "$run" = "poetry run" ]; then cmd test "poetry run pytest -q"
    else cmd test "$run --with pytest pytest -q"; fi
  fi
  if has ruff.toml || has .ruff.toml || grep -qs '^\[tool\.ruff' pyproject.toml; then cmd lint "ruff check ."; fi
  if has mypy.ini || grep -qs '^\[tool\.mypy' pyproject.toml; then
    if [ "$run" = "poetry run" ]; then cmd lint "poetry run mypy ."; else cmd lint "$run --with mypy mypy ."; fi
  fi
  if { has pyproject.toml && grep -qs '^\[build-system\]' pyproject.toml; } || has setup.py; then
    if [ "$run" = "poetry run" ]; then cmd build "poetry build"; else cmd build "uv build"; fi
  fi
fi
if has Cargo.toml; then cmd build "cargo build"; cmd test "cargo test"; cmd lint "cargo clippy -- -D warnings"; fi
if has pom.xml; then cmd build "mvn -q -DskipTests package"; cmd test "mvn -q test"; fi
if has build.gradle || has build.gradle.kts; then g=gradle; has gradlew && g=./gradlew; cmd build "$g build -x test"; cmd test "$g test"; fi
for mk in Makefile GNUmakefile; do
  has "$mk" || continue
  grep -E '^[A-Za-z0-9][A-Za-z0-9_.-]*:([^=]|$)' "$mk" | sed -E 's/:.*//' | sort -u | while read -r t; do
    case "$t" in
      *test*) cmd test "make $t" ;;
      *lint*|*vet*|check|*-check|check-*) cmd lint "make $t" ;;
      build|all) cmd build "make $t" ;;
    esac
  done
done
if has justfile; then
  grep -E '^[A-Za-z0-9_-]+[^:=]*:([^=]|$)' justfile | sed -E 's/^([A-Za-z0-9_-]+).*/\1/' | sort -u | while read -r t; do
    case "$t" in *test*) cmd test "just $t" ;; *lint*|*check*) cmd lint "just $t" ;; build) cmd build "just $t" ;; esac
  done
fi
if ls .github/workflows/*.y*ml >/dev/null 2>&1; then
  grep -hE '^[[:space:]]*(-[[:space:]]*)?run:[[:space:]]*[^|>[:space:]]' .github/workflows/*.y*ml 2>/dev/null \
    | sed -E 's/^[[:space:]]*(-[[:space:]]*)?run:[[:space:]]*//' | awk '!seen[$0]++' | head -n 10 \
    | while IFS= read -r c; do cmd ci "$c"; done
fi
awk -F'\t' '!seen[$0]++' "$TMP/commands" > "$TMP/commands.u" && mv "$TMP/commands.u" "$TMP/commands"

# ------------------------------------------------------------------ entrypoints
grep -E '(^|/)cmd/[^/]+/main\.go$|^main\.go$|(^|/)__main__\.py$|^(manage|main|app|wsgi|asgi|server|cli)\.py$|^src/[^/]+/(main|app|cli|server)\.py$|^(src/)?(index|main|server|app)\.(ts|tsx|js|mjs)$|^src/main\.rs$|^src/bin/[^/]+\.rs$|(^|/)Dockerfile[^/]*$|^(docker-)?compose[^/]*\.ya?ml$' "$FILES" \
  | grep -vE '(^|/)(tests?|testdata|examples?|fixtures)/' | head -n 25 > "$TMP/entrypoints" || true
if has package.json; then
  grep -oE '"(main|module)"[[:space:]]*:[[:space:]]*"[^"]+"' package.json 2>/dev/null | head -n 2 \
    | sed -E 's/"([^"]+)"[[:space:]]*:[[:space:]]*"([^"]+)"/package.json \1: \2/' >> "$TMP/entrypoints" || true
fi
if has pyproject.toml; then
  awk '/^\[project\.scripts\]/{on=1; next} /^\[/{on=0} on && NF {gsub(/"/, ""); print "pyproject script: " $0}' pyproject.toml \
    | head -n 5 >> "$TMP/entrypoints"
fi

# ------------------------------------------------------------------ tree (depth 1 and 2 dirs with file counts)
awk -F/ 'NF > 1 { top[$1]++ } END { for (d in top) printf "%d\t%s\n", top[d], d }' "$FILES" \
  | sort -t "$(printf '\t')" -k2 | head -n "$MAX_ENTRIES" > "$TMP/tree_top"
awk -F/ 'NF > 2 { sub2[$1 "/" $2]++ } END { for (d in sub2) printf "%d\t%s\n", sub2[d], d }' "$FILES" \
  | sort -t "$(printf '\t')" -k1,1nr -k2 | head -n "$MAX_ENTRIES" > "$TMP/tree_sub"
sort -t "$(printf '\t')" -k2 "$TMP/tree_top" "$TMP/tree_sub" > "$TMP/tree"
awk -F/ 'NF == 1' "$FILES" | head -n 40 > "$TMP/root_files"

# ------------------------------------------------------------------ open markers (TO-DO, FIX-ME, X-XX, HA-CK)
MARK_RE='(^|[^A-Za-z])(TO''DO|FIX''ME|X''XX|HA''CK)([^A-Za-z]|$)'  # split so this file does not count itself
if $IS_GIT; then
  git -c core.quotePath=false grep --untracked -I -c -E "$MARK_RE" -- . 2>/dev/null > "$TMP/marks_raw" || true
else
  # shellcheck disable=SC2046,SC2086  # word splitting builds the --exclude-dir list on purpose
  grep -rIcE "$MARK_RE" $(for d in $PRUNE_DIRS; do printf -- '--exclude-dir=%s ' "$d"; done) . 2>/dev/null \
    | sed 's|^\./||' | awk -F: '$NF > 0' > "$TMP/marks_raw" || true
fi
MARK_TOTAL=$(awk -F: '{ s += $NF } END { print s + 0 }' "$TMP/marks_raw")
awk -F: '{ c = $NF; sub(/:[0-9]+$/, ""); printf "%d\t%s\n", c, $0 }' "$TMP/marks_raw" | sort -k1,1nr -k2 | head -n 10 > "$TMP/marks_top"

# ------------------------------------------------------------------ git
BRANCH="" HEAD_SHA="" DIRTY=0 COMMITS_30D=0 LAST_COMMIT=""
: > "$TMP/remotes"; : > "$TMP/log"
if $IS_GIT; then
  BRANCH=$(git symbolic-ref --short -q HEAD 2>/dev/null || git rev-parse --short HEAD 2>/dev/null || true)
  if git rev-parse -q --verify HEAD >/dev/null 2>&1; then
    HEAD_SHA=$(git rev-parse --short HEAD)
    # history and dirty files are scoped to PATH, so a monorepo package gets its own activity
    LAST_COMMIT=$(git log -1 --date=iso-strict --pretty=format:%ad -- .)
    COMMITS_30D=$(git rev-list --count --since=30.days HEAD -- .)
    git log -n "$COMMITS" --date=short --pretty=tformat:'%h%x09%ad%x09%an%x09%s' -- . > "$TMP/log"
  fi
  DIRTY=$(git status --porcelain -- . 2>/dev/null | wc -l | tr -d ' ')
  git remote -v 2>/dev/null | awk '$3 == "(fetch)" { print $1 "\t" $2 }' \
    | sed -E 's#(https?://)[^@/]+@#\1***@#' > "$TMP/remotes"
fi

# ------------------------------------------------------------------ output
# Paths, commit subjects and author names are raw bytes (core.quotePath=false); bytes that are not
# UTF-8 would make the JSON invalid, so the whole output drops them (iconv exits 1 when it did).
utf8_clean() {
  if command -v iconv >/dev/null 2>&1; then
    iconv -f UTF-8 -t UTF-8 -c || [ $? -eq 1 ]
  else
    LC_ALL=C tr -d '\200-\377'
  fi
}

json_str() {
  local s
  s=$(printf '%s' "$1" | LC_ALL=C tr -d '\000-\010\013\014\016-\037')
  s=${s//\\/\\\\}; s=${s//\"/\\\"}; s=${s//$'\t'/\\t}; s=${s//$'\n'/\\n}; s=${s//$'\r'/\\r}
  printf '"%s"' "$s"
}

# json_rows FILE "key:type key:type ..."  (TSV rows -> array of objects; type s=string n=number).
# The last field takes the rest of the line, so free text (commit subjects, paths) may contain tabs.
json_rows() {
  local file=$1 spec=$2 first=1 line i f key typ val n
  # shellcheck disable=SC2086  # one spec item per line
  n=$(printf '%s\n' $spec | wc -l | tr -d ' ')
  printf '['
  while IFS= read -r line || [ -n "$line" ]; do
    [ -n "$line" ] || continue
    [ $first -eq 1 ] || printf ','
    first=0; i=1; printf '{'
    for f in $spec; do
      key=${f%%:*}; typ=${f##*:}
      if [ "$i" -eq "$n" ]; then val=$(printf '%s' "$line" | cut -f "$i-"); else val=$(printf '%s' "$line" | cut -f "$i"); fi
      [ $i -eq 1 ] || printf ','
      if [ "$typ" = n ]; then printf '"%s":%s' "$key" "${val:-0}"; else printf '"%s":%s' "$key" "$(json_str "$val")"; fi
      i=$((i + 1))
    done
    printf '}'
  done < "$file"
  printf ']'
}

json_list() {
  local file=$1 first=1 line
  printf '['
  while IFS= read -r line || [ -n "$line" ]; do
    [ -n "$line" ] || continue
    [ $first -eq 1 ] || printf ','
    first=0; json_str "$line"
  done < "$file"
  printf ']'
}

commands_of() { awk -F'\t' -v k="$1" '$1 == k { print $2 }' "$TMP/commands" > "$TMP/cmd_$1"; json_list "$TMP/cmd_$1"; }

emit_json() {
  printf '{"root":%s,"generated_at":%s,"files_total":%s,' "$(json_str "$ROOT_ABS")" \
    "$(json_str "$(date -u +%Y-%m-%dT%H:%M:%SZ)")" "$FILES_TOTAL"
  printf '"git":{"is_repo":%s,"branch":%s,"head":%s,"last_commit":%s,"commits_30d":%s,"dirty_files":%s,"remotes":%s,"recent_commits":%s},' \
    "$IS_GIT" "$(json_str "$BRANCH")" "$(json_str "$HEAD_SHA")" "$(json_str "$LAST_COMMIT")" "${COMMITS_30D:-0}" "${DIRTY:-0}" \
    "$(json_rows "$TMP/remotes" "name:s url:s")" "$(json_rows "$TMP/log" "hash:s date:s author:s subject:s")"
  printf '"languages":%s,"manifests":%s,' "$(json_rows "$TMP/languages" "files:n language:s")" "$(json_list "$TMP/manifests")"
  printf '"commands":{"build":%s,"test":%s,"lint":%s,"ci":%s},' \
    "$(commands_of build)" "$(commands_of test)" "$(commands_of lint)" "$(commands_of ci)"
  printf '"entrypoints":%s,"tree":%s,"root_files":%s,' "$(json_list "$TMP/entrypoints")" \
    "$(json_rows "$TMP/tree" "files:n path:s")" "$(json_list "$TMP/root_files")"
  printf '"todos":{"total":%s,"top_files":%s}}\n' "$MARK_TOTAL" "$(json_rows "$TMP/marks_top" "count:n path:s")"
}

section() { printf '\n## %s\n' "$1"; }
bullets() { if [ -s "$1" ]; then sed 's/^/- /' "$1"; else echo "- (nenhum)"; fi; }

emit_md() {
printf '# Mapa do repositório: %s\n\n' "$ROOT_ABS"
printf -- '- Arquivos: %s · git: %s · branch: %s · HEAD: %s · último commit: %s · commits (30d): %s · arquivos sujos: %s\n' \
  "$FILES_TOTAL" "$IS_GIT" "${BRANCH:--}" "${HEAD_SHA:--}" "${LAST_COMMIT:--}" "$COMMITS_30D" "$DIRTY"
section "Linguagens (arquivos)"; awk -F'\t' '{ printf "- %s: %s\n", $2, $1 }' "$TMP/languages"
section "Manifests"; bullets "$TMP/manifests"
section "Comandos"
for k in build test lint ci; do
  awk -F'\t' -v k="$k" '$1 == k { printf "- %s: `%s`\n", k, $2 }' "$TMP/commands"
done
[ -s "$TMP/commands" ] || echo "- (nenhum detectado; inspecione README/CI)"
section "Entrypoints"; bullets "$TMP/entrypoints"
section "Árvore (diretórios: arquivos)"; awk -F'\t' '{ printf "- %s/: %s\n", $2, $1 }' "$TMP/tree"
section "Arquivos na raiz"; bullets "$TMP/root_files"
section "Marcadores TO""DO/FIX""ME ($MARK_TOTAL)"; awk -F'\t' '{ printf "- %s: %s\n", $2, $1 }' "$TMP/marks_top"
section "Commits recentes"; awk -F'\t' '{ printf "- %s %s %s — %s\n", $1, $2, $3, $4 }' "$TMP/log"
section "Remotes"; awk -F'\t' '{ printf "- %s: %s\n", $1, $2 }' "$TMP/remotes"
}

if [ "$FORMAT" = json ]; then emit_json > "$TMP/out"; else emit_md > "$TMP/out"; fi
utf8_clean < "$TMP/out"
