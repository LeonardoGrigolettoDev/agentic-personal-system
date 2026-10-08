#!/usr/bin/env bash
# Optional end-to-end checks against real PostgreSQL (no Docker), run by run.sh when both variables are set:
# 1. railway-db-migrate.sh: a throwaway cluster as the "Railway" target, a real copy with --client host,
#    verification, and a re-run that proves the non-empty guard;
# 2. the Railway postgres first boot: the image's initdb scripts, staged like infra/railway/postgres/Dockerfile,
#    run like the docker-library entrypoint against a server listening on the Unix socket only, with a TCP
#    PGHOST in the environment (it must be ignored, or the init aborts and the cluster has no roles).
#   RAILWAY_IT_PGBIN=<dir with initdb/pg_ctl/psql/pg_dump/pg_restore, PG >= 17, pgvector installed>
#   RAILWAY_IT_SOURCE_URL=postgresql://aios:<pw>@127.0.0.1:<port>/aios   (a migrated aios database; read only)
set -euo pipefail
cd "$(dirname "$0")/../../.."
: "${RAILWAY_IT_PGBIN:?}" "${RAILWAY_IT_SOURCE_URL:?}"
export PATH="$RAILWAY_IT_PGBIN:$PATH"

tmp=$(mktemp -d "${TMPDIR:-/tmp}/aios-railway-it.XXXXXX")
port=$((55400 + RANDOM % 90))
cleanup() {
  pg_ctl -D "$tmp/data" stop -m immediate >/dev/null 2>&1 || true
  pg_ctl -D "$tmp/init" stop -m immediate >/dev/null 2>&1 || true
  rm -rf "$tmp"
}
trap cleanup EXIT

initdb -D "$tmp/data" -U postgres --auth=trust -E UTF8 >/dev/null
pg_ctl -D "$tmp/data" -o "-p $port -c listen_addresses=127.0.0.1 -k $tmp" -l "$tmp/log" -w start >/dev/null
target="postgresql://postgres@127.0.0.1:$port/postgres"
args=(--source "$RAILWAY_IT_SOURCE_URL" --client host --target "$target" --dbs aios)
export MIGRATE_WORK_DIR="$tmp/work" AIOS_DB_PASSWORD=it-aios-pw AIOS_READER_PASSWORD=it-reader-pw

if ! bash infra/scripts/railway-db-migrate.sh "${args[@]}" >"$tmp/dry.log" 2>&1 || ! grep -q 'DRY-RUN' "$tmp/dry.log"; then
  cat "$tmp/dry.log"; echo "FAIL dry run"; exit 1
fi
[[ "$(psql "$target" -Atc "SELECT count(*) FROM pg_database WHERE datname = 'aios'")" == 0 ]] ||
  { echo "FAIL dry run created the database"; exit 1; }
bash infra/scripts/railway-db-migrate.sh "${args[@]}" --apply --yes >"$tmp/apply.log" 2>&1 ||
  { cat "$tmp/apply.log"; echo "FAIL apply"; exit 1; }
grep -q 'row counts identical' "$tmp/apply.log" || { cat "$tmp/apply.log"; echo "FAIL verification"; exit 1; }
owners=$(psql "postgresql://postgres@127.0.0.1:$port/aios" -Atc \
  "SELECT string_agg(DISTINCT tableowner, ',') FROM pg_tables WHERE schemaname = 'public'")
[[ "$owners" == aios ]] || { echo "FAIL table owners: $owners"; exit 1; }
PGPASSWORD=it-aios-pw psql -h 127.0.0.1 -p "$port" -U aios -d aios -Atc "SELECT '[1,2]'::vector <-> '[1,3]'::vector" >/dev/null ||
  { echo "FAIL aios login / pgvector"; exit 1; }
if bash infra/scripts/railway-db-migrate.sh "${args[@]}" >"$tmp/rerun.log" 2>&1; then
  echo "FAIL re-run on a populated target was not refused"; exit 1
fi
grep -q 'refusing to overwrite' "$tmp/rerun.log" || { cat "$tmp/rerun.log"; echo "FAIL guard message"; exit 1; }

# --- 2. first boot of the Railway postgres image ------------------------------------------------------------
# A bare `psql` in the init scripts uses libpq's compiled-in socket directory, so the server listens there.
probe=$(env -u PGHOST -u PGHOSTADDR psql -X -p 1 -c '' 2>&1 || true)   # fails by design; the error names the socket
sockdir=$(sed -n 's|.*socket "\(.*\)/\.s\.PGSQL\.1".*|\1|p' <<<"$probe" | head -n1)
[[ -n "$sockdir" && -d "$sockdir" && -w "$sockdir" ]] || { echo "FAIL cannot use libpq's default socket dir '$sockdir'"; exit 1; }
iport=$((55500 + RANDOM % 90))
while [[ -e "$sockdir/.s.PGSQL.$iport" ]]; do iport=$((iport + 1)); done
initdb -D "$tmp/init" -U postgres --auth=trust -E UTF8 >/dev/null
pg_ctl -D "$tmp/init" -o "-p $iport -c listen_addresses= -k $sockdir" -l "$tmp/init.log" -w start >/dev/null
stage_initdb() { # dir [with-guard]
  mkdir -p "$1"
  cp -p infra/docker/postgres/initdb/* "$1/"
  [[ -z "${2:-}" ]] || install -m 0644 infra/railway/postgres/initdb/00-local-socket.sh "$1/00-local-socket.sh"
}
first_boot() { # dir: the service's environment, plus the TCP PGHOST the service used to set
  env POSTGRES_USER=postgres POSTGRES_DB=postgres PGUSER=postgres PGPORT="$iport" PGCONNECT_TIMEOUT=5 \
    PGHOST=postgres.railway.internal.invalid AIOS_DB_PASSWORD=it-aios-pw AIOS_READER_PASSWORD=it-reader-pw \
    LITELLM_DB_PASSWORD=it-litellm-pw LANGFUSE_DB_PASSWORD=it-langfuse-pw \
    bash infra/railway/tests/initdb-emulate.sh "$1"
}
stage_initdb "$tmp/initdb-unguarded"
if first_boot "$tmp/initdb-unguarded" >"$tmp/unguarded.log" 2>&1; then
  echo "FAIL control: the init scripts reached a socket-only server through a TCP PGHOST"; exit 1
fi
stage_initdb "$tmp/initdb" guard
first_boot "$tmp/initdb" >"$tmp/firstboot.log" 2>&1 || { cat "$tmp/firstboot.log"; echo "FAIL first boot with the guard"; exit 1; }
isql() { psql -X -h "$sockdir" -p "$iport" -U postgres -qAt "$@"; }
[[ "$(isql -c "SELECT string_agg(rolname, ',' ORDER BY rolname) FROM pg_roles WHERE rolname IN ('aios', 'aios_reader', 'litellm', 'langfuse')")" == aios,aios_reader,langfuse,litellm ]] &&
  [[ "$(isql -c "SELECT string_agg(datname, ',' ORDER BY datname) FROM pg_database WHERE datname IN ('aios', 'litellm', 'langfuse')")" == aios,langfuse,litellm ]] &&
  [[ "$(isql -d aios -c "SELECT count(*) FROM pg_extension WHERE extname IN ('vector', 'pg_trgm', 'unaccent')")" == 3 ]] ||
  { cat "$tmp/firstboot.log"; echo "FAIL first boot did not create the roles, databases and extensions"; exit 1; }
echo "ok   integration: real PG copy, owners=aios, aios login + pgvector, populated target refused; first boot initdb on the socket despite a TCP PGHOST"
