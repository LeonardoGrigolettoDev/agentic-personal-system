#!/usr/bin/env bash
# Copy the local Postgres into the Railway Postgres (ARCHITECTURE §25.3): pg_dump -Fc -> pg_restore -> pending
# migrations -> exact row-count comparison. DRY-RUN by default: only read-only preflight queries run on both
# sides. --apply writes (after typing the target host, or --yes) and never touches a non-empty target database.
#
#   TARGET_DATABASE_URL=postgresql://postgres:<pw>@127.0.0.1:15432/postgres infra/scripts/railway-db-migrate.sh
#   infra/scripts/railway-db-migrate.sh --target <url> --apply          # after reviewing the dry run
#   infra/scripts/railway-db-migrate.sh --dump-only                     # dumps for `railway volume files upload`
#
# The target URL must be a superuser on the Railway Postgres. Prefer an SSH tunnel (`railway connect postgres
# --tunnel-only -P 15432`, or `railway ssh config -s postgres` + `ssh -N -L 15432:127.0.0.1:5432 railway-postgres`);
# a public TCP proxy without sslmode is refused. Nothing in the cloud may be connected to the target databases
# while --apply runs (RUNBOOK §9.2: litellm/hermes in AIOS_MAINTENANCE, decision/knowledge down).
set -euo pipefail
cd "$(dirname "$0")/../.."

usage() {
  cat <<'EOF'
usage: railway-db-migrate.sh [--target URL] [--dbs aios,litellm] [--source compose|URL] [--client docker|host]
                             [--apply [--yes]] [--dump-only] [--allow-writers] [--allow-insecure]
  --target URL      superuser URL of the Railway Postgres (default: $TARGET_DATABASE_URL)
  --dbs LIST        databases to copy (default aios,litellm; add langfuse only if it is self-hosted)
  --source S        compose = `docker compose exec postgres` (default) | a superuser URL for host pg_dump
  --client C        docker = psql/pg_restore from $PG_CLIENT_IMAGE (default) | host = binaries on PATH
  --apply           perform the copy (otherwise dry run); --yes skips the typed confirmation
  --dump-only       only write the dumps under backups/railway-migrate-<ts>/ (no target needed)
  --allow-writers   do not require the local writers (litellm/decision/knowledge/hermes) to be stopped nor the
                    target databases to be free of other sessions
  --allow-insecure  allow a non-local target without sslmode=require|verify-*
Role passwords for missing roles come from AIOS_DB_PASSWORD, AIOS_READER_PASSWORD, LITELLM_DB_PASSWORD and
LANGFUSE_DB_PASSWORD (environment first, then .env).
EOF
}

PG_CLIENT_IMAGE="${PG_CLIENT_IMAGE:-pgvector/pgvector:0.8.7-pg17-trixie}"
TARGET="${TARGET_DATABASE_URL:-}"
DBS="aios,litellm"
SOURCE=compose
CLIENT=docker
APPLY=0 YES=0 DUMP_ONLY=0 ALLOW_WRITERS=0 ALLOW_INSECURE=0
# filled by url_env: T_* = target, S_* = source (only with --source <url>)
T_HOST="" T_PORT="" T_DATABASE="" T_SSLMODE="" S_HOST="" S_PORT=""
while (($#)); do
  case "$1" in
    --target) TARGET="${2:?--target needs a URL}"; shift ;;
    --dbs) DBS="${2:?--dbs needs a list}"; shift ;;
    --source) SOURCE="${2:?--source needs compose or a URL}"; shift ;;
    --client) CLIENT="${2:?--client needs docker or host}"; shift ;;
    --apply) APPLY=1 ;;
    --yes) YES=1 ;;
    --dump-only) DUMP_ONLY=1 ;;
    --allow-writers) ALLOW_WRITERS=1 ;;
    --allow-insecure) ALLOW_INSECURE=1 ;;
    -h | --help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

say() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
[[ "$CLIENT" == docker || "$CLIENT" == host ]] || die "--client must be docker or host"

declare -A OWNER=([aios]=aios [litellm]=litellm [langfuse]=langfuse)
declare -A PWVAR=([aios]=AIOS_DB_PASSWORD [aios_reader]=AIOS_READER_PASSWORD [litellm]=LITELLM_DB_PASSWORD
  [langfuse]=LANGFUSE_DB_PASSWORD)
IFS=',' read -r -a DB_LIST <<<"${DBS// /}"
((${#DB_LIST[@]})) || die "--dbs is empty"
for db in "${DB_LIST[@]}"; do [[ -n "${OWNER[$db]:-}" ]] || die "unsupported database '$db' (aios, litellm, langfuse)"; done

# --- URL -> libpq environment (keeps passwords out of argv) -------------------------------------------------
url_env() { # url prefix -> prints shell assignments <prefix>HOST=... (values shell-quoted)
  python3 - "$1" "$2" <<'PY'
import shlex, sys
from urllib.parse import unquote, urlsplit, parse_qs
url, p = sys.argv[1], sys.argv[2]
u = urlsplit(url)
if u.scheme not in ("postgres", "postgresql") or not u.hostname:
    sys.exit(f"not a postgresql:// URL with a host: {u.scheme}://{u.hostname or ''}")
q = {k: v[-1] for k, v in parse_qs(u.query).items()}
vals = {"HOST": u.hostname, "PORT": str(u.port or 5432), "USER": unquote(u.username or "postgres"),
        "PASSWORD": unquote(u.password or ""), "DATABASE": unquote(u.path.lstrip("/")) or "postgres",
        "SSLMODE": q.get("sslmode", "")}
for k, v in vals.items():
    print(f"{p}{k}={shlex.quote(v)}")
PY
}

with_pg() { # prefix db cmd... : run cmd with PG* exported from <prefix>* and PGDATABASE=db
  local p=$1 db=$2; shift 2
  local h="${p}HOST" po="${p}PORT" u="${p}USER" pw="${p}PASSWORD" s="${p}SSLMODE"
  (
    export PGHOST="${!h}" PGPORT="${!po}" PGUSER="${!u}" PGPASSWORD="${!pw}" PGDATABASE="$db" \
      PGCONNECT_TIMEOUT="${PGCONNECT_TIMEOUT:-15}" PGAPPNAME=aios-railway-migrate
    [[ -n "${!s}" ]] && export PGSSLMODE="${!s}"
    "$@"
  )
}

client() { # run a postgres client tool against the PG* environment (stdin passes through)
  if [[ "$CLIENT" == host ]]; then
    "$@"
  else
    local mounts=()
    [[ -d "${WORK:-}" ]] && mounts=(-v "$(realpath "$WORK"):/work")
    docker run --rm -i --network host -e PGHOST -e PGPORT -e PGUSER -e PGPASSWORD -e PGDATABASE -e PGSSLMODE \
      -e PGCONNECT_TIMEOUT -e PGAPPNAME -e PGROLEPW ${mounts[@]+"${mounts[@]}"} "$PG_CLIENT_IMAGE" "$@"
  fi
}
work_path() { if [[ "$CLIENT" == host ]]; then echo "$(realpath "$WORK")/$1"; else echo "/work/$1"; fi; }

tq() { with_pg T_ "$1" client psql -X -v ON_ERROR_STOP=1 -qAt -c "$2"; }   # target query: db sql
sq() { # source query: db sql
  if [[ "$SOURCE" == compose ]]; then
    docker compose exec -T postgres psql -X -v ON_ERROR_STOP=1 -U postgres -d "$1" -qAt -c "$2"
  else
    with_pg S_ "$1" psql -X -v ON_ERROR_STOP=1 -qAt -c "$2"
  fi
}
source_dump() { # db -> custom-format dump on stdout
  if [[ "$SOURCE" == compose ]]; then
    docker compose exec -T postgres pg_dump -U postgres -Fc "$1"
  else
    with_pg S_ "$1" pg_dump -Fc
  fi
}

COUNTS_SQL="/* aios:counts */ SELECT format('%I.%I', schemaname, tablename) || '|' ||
  (xpath('/row/c/text()', query_to_xml(format('SELECT count(*) AS c FROM %I.%I', schemaname, tablename),
   false, true, '')))[1]::text FROM pg_tables
  WHERE schemaname NOT IN ('pg_catalog', 'information_schema') ORDER BY 1"

# --- source preflight ---------------------------------------------------------------------------------------
if [[ "$SOURCE" == compose ]]; then
  command -v docker >/dev/null || die "docker not found (or pass --source <url>)"
  [[ -n "$(docker compose ps -q --status running postgres 2>/dev/null)" ]] || die "local postgres is not running (make up-core)"
else
  vars=$(url_env "$SOURCE" S_) || die "bad --source URL"
  eval "$vars"
fi
src_ver=$(sq postgres "/* aios:version */ SHOW server_version_num") || die "cannot query the source server"
say "source: $([[ $SOURCE == compose ]] && echo "docker compose postgres" || echo "$S_HOST:$S_PORT"), PostgreSQL $src_ver"
for db in "${DB_LIST[@]}"; do
  size=$(sq postgres "/* aios:dbsize */ SELECT pg_size_pretty(pg_database_size('$db'))") ||
    die "source database '$db' not found"
  say "  $db: $size"
done

writers=""
if [[ "$SOURCE" == compose ]]; then
  writers=$(docker compose --profile agent ps --status running --services 2>/dev/null |
    grep -xE 'litellm|decision|knowledge|hermes' | tr '\n' ' ' || true)
  [[ -z "$writers" ]] || say "  WARNING: still running and writing: $writers(stop them before --apply: docker compose stop $writers)"
fi

WORK="${MIGRATE_WORK_DIR:-backups}/railway-migrate-$(date -u +%Y%m%dT%H%M%SZ)"
dump_all() {
  mkdir -p "$WORK"
  chmod 700 "$(dirname "$WORK")" "$WORK" 2>/dev/null || true
  for db in "${DB_LIST[@]}"; do
    source_dump "$db" >"$WORK/$db.dump" || die "pg_dump $db failed"
    [[ -s "$WORK/$db.dump" ]] || die "pg_dump $db produced an empty file"
    say "dumped $db -> $WORK/$db.dump ($(du -h "$WORK/$db.dump" | cut -f1))"
  done
}

if ((DUMP_ONLY)); then
  dump_all
  cat <<EOF
Plan B (no tunnel available): restore inside the postgres container as the superuser, owners kept. First confirm
that the first boot's initdb created the roles and databases and that nothing has written to them yet:
  railway ssh -s postgres -- psql -X -U postgres -Atc "SELECT datname FROM pg_database ORDER BY 1"   # aios, litellm, ...
  railway ssh -s postgres -- psql -X -U postgres -d litellm -Atc "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'"   # 0
Then, per dump (the volume root is /var/lib/postgresql/data):
EOF
  for db in "${DB_LIST[@]}"; do
    cat <<EOF
  railway volume files --volume pgdata upload $WORK/$db.dump /$db.dump
  railway ssh -s postgres -- pg_restore -U postgres -d $db --exit-on-error --single-transaction /var/lib/postgresql/data/$db.dump
  railway ssh -s postgres -- rm /var/lib/postgresql/data/$db.dump
EOF
  done
  say "Finally compare row counts by hand (RUNBOOK §9.3). The dumps carry the migrations applied locally: run make migrate before --dump-only."
  exit 0
fi

# --- target preflight (read-only) ---------------------------------------------------------------------------
[[ -n "$TARGET" ]] || die "no target: set TARGET_DATABASE_URL or pass --target (see --help)"
vars=$(url_env "$TARGET" T_) || die "bad target URL"
eval "$vars"
case "$T_HOST" in
  localhost | 127.0.0.1 | ::1) ;;
  *) [[ "$T_SSLMODE" =~ ^(require|verify-ca|verify-full)$ ]] || ((ALLOW_INSECURE)) ||
    die "target $T_HOST is remote without sslmode=require: use \`railway connect postgres --tunnel-only\` (or --allow-insecure)" ;;
esac
[[ "$CLIENT" == host ]] || command -v docker >/dev/null || die "docker not found (or use --client host)"
command -v python3 >/dev/null || die "python3 is required"

server=$(tq "$T_DATABASE" "/* aios:server */ SELECT current_setting('server_version_num'), current_user, (SELECT rolsuper FROM pg_roles WHERE rolname = current_user)") ||
  die "cannot connect to the target $T_HOST:$T_PORT"
IFS='|' read -r tgt_ver tgt_user tgt_super <<<"$server"
say "target: $T_HOST:$T_PORT as $tgt_user, PostgreSQL $tgt_ver"
[[ "$tgt_super" == t ]] || die "the target user must be a superuser (roles, extensions, --role restore)"
((tgt_ver / 10000 >= src_ver / 10000)) || die "target major $((tgt_ver / 10000)) is older than source $((src_ver / 10000))"

exts=$(tq "$T_DATABASE" "/* aios:extensions */ SELECT name FROM pg_available_extensions WHERE name IN ('vector', 'pg_trgm', 'unaccent') ORDER BY 1") ||
  die "cannot list extensions on the target"
for e in vector pg_trgm unaccent; do
  grep -qx "$e" <<<"$exts" || die "extension '$e' is not available on the target$([[ $e == vector ]] && echo ': deploy infra/railway/postgres (pgvector image), not the stock Railway Postgres')"
done
say "  extensions available: $(tr '\n' ' ' <<<"$exts")"

db_in=$(printf "'%s'," "${DB_LIST[@]}" | sed 's/,$//')
existing=$(tq "$T_DATABASE" "/* aios:databases */ SELECT datname FROM pg_database WHERE datname IN ($db_in) ORDER BY 1") ||
  die "cannot list databases on the target"
for db in "${DB_LIST[@]}"; do
  if grep -qx "$db" <<<"$existing"; then
    n=$(tq "$db" "/* aios:tables */ SELECT count(*) FROM pg_tables WHERE schemaname NOT IN ('pg_catalog', 'information_schema')") ||
      die "cannot inspect target database $db"
    if [[ "$n" != 0 && "$db" == litellm ]]; then
      die "target database 'litellm' already has $n tables: the cloud LiteLLM booted before the copy and ran its migrations (shared AIOS_MAINTENANCE was not 1). Put litellm in maintenance, wait until nothing is connected, recreate the database on the target (DROP DATABASE litellm; CREATE DATABASE litellm OWNER litellm;) and run this again"
    fi
    [[ "$n" == 0 ]] || die "target database '$db' already has $n tables; refusing to overwrite (DROP it first if it is a failed attempt)"
    say "  $db: exists, empty"
  else
    say "  $db: will be created (owner ${OWNER[$db]})"
  fi
done

# cloud services still connected would write during the restore or hold the databases open
sessions=$(tq "$T_DATABASE" "/* aios:sessions */ SELECT datname || ' ' || coalesce(nullif(application_name, ''), '-') || ' ' || coalesce(host(client_addr), 'local') FROM pg_stat_activity WHERE datname IN ($db_in) AND pid <> pg_backend_pid() AND backend_type = 'client backend' ORDER BY 1") ||
  die "cannot list sessions on the target"
if [[ -n "$sessions" ]]; then
  say "  WARNING: other sessions on the target databases (database application client):"
  while IFS= read -r line; do say "    $line"; done <<<"$sessions"
fi

roles=()
for db in "${DB_LIST[@]}"; do roles+=("${OWNER[$db]}"); done
[[ " ${DB_LIST[*]} " == *" aios "* ]] && roles+=(aios_reader)
cat <<EOF

Plan:
  1. ensure roles ${roles[*]} and databases ${DB_LIST[*]} on $T_HOST (missing ones only)
  2. extensions vector, pg_trgm, unaccent in aios (as superuser)
  3. pg_dump -Fc ${DB_LIST[*]} -> $WORK/ (kept for rollback)
  4. pg_restore --no-owner --role=<owner> --single-transaction --exit-on-error (extension entries filtered)
  5. compare exact row counts per table (source vs restored target)
  6. pending migrations/*.sql on aios as role aios, then ANALYZE
EOF

if ((!APPLY)); then
  say ""
  say "DRY-RUN: nothing was written. Re-run with --apply after stopping the writers."
  exit 0
fi

# --- apply --------------------------------------------------------------------------------------------------
[[ -z "$writers" ]] || ((ALLOW_WRITERS)) || die "stop the writers first: docker compose stop $writers(or --allow-writers)"
[[ -z "$sessions" ]] || ((ALLOW_WRITERS)) ||
  die "cloud sessions are connected to the target databases: AIOS_MAINTENANCE=1 for litellm/hermes and railway down -s decision/knowledge (RUNBOOK §9.2), or --allow-writers"
if ((!YES)); then
  [[ -t 0 ]] || die "no terminal for the confirmation prompt: pass --yes"
  read -r -p "Type the target host ($T_HOST) to copy ${DB_LIST[*]} into it: " answer
  [[ "$answer" == "$T_HOST" ]] || die "confirmation did not match; nothing written"
fi

env_or_dotenv() { # NAME -> value from the environment, else from .env (no sourcing)
  local v="${!1:-}"
  [[ -n "$v" || ! -f .env ]] || v=$(sed -n "s/^$1=//p" .env | tail -n1)
  printf '%s' "$v"
}

for role in "${roles[@]}"; do
  pw=$(env_or_dotenv "${PWVAR[$role]}")
  [[ -n "$pw" ]] || die "${PWVAR[$role]} is empty (needed if role $role is missing on the target)"
  PGROLEPW="$pw" with_pg T_ "$T_DATABASE" client psql -X -v ON_ERROR_STOP=1 -qAt -v role="$role" >/dev/null <<'SQL' ||
/* aios:create-role */
\getenv pw PGROLEPW
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'role', :'pw')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'role') \gexec
SQL
    die "cannot ensure role $role"
done
say "roles ok: ${roles[*]}"

for db in "${DB_LIST[@]}"; do
  if ! grep -qx "$db" <<<"$existing"; then
    tq "$T_DATABASE" "/* aios:create-db */ CREATE DATABASE \"$db\" OWNER \"${OWNER[$db]}\"" >/dev/null
    say "created database $db"
  fi
done
if printf '%s\n' "${DB_LIST[@]}" | grep -qx aios; then
  tq aios "/* aios:extensions-create */ CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS pg_trgm; CREATE EXTENSION IF NOT EXISTS unaccent" >/dev/null
fi

dump_all
for db in "${DB_LIST[@]}"; do
  client_dump=$(work_path "$db.dump")
  with_pg T_ "$db" client pg_restore -l "$client_dump" |
    grep -vE '; [0-9]+ [0-9]+ (EXTENSION - |COMMENT - EXTENSION |COMMENT - SCHEMA public)' >"$WORK/$db.toc" ||
    die "cannot read the TOC of $db.dump"
  with_pg T_ "$db" client pg_restore --no-owner --role="${OWNER[$db]}" --single-transaction --exit-on-error \
    -L "$(work_path "$db.toc")" -d "$db" "$client_dump" || die "pg_restore $db failed (target left empty: single transaction)"
  say "restored $db"
done

# the restore is verified before pending migrations change the schema
status=0
for db in "${DB_LIST[@]}"; do
  src_counts=$(sq "$db" "$COUNTS_SQL") || die "cannot count rows in source $db"
  tgt_counts=$(tq "$db" "$COUNTS_SQL") || die "cannot count rows in target $db"
  if diff <(echo "$src_counts") <(echo "$tgt_counts") >"$WORK/$db.counts.diff"; then
    say "verified $db: row counts identical in $(grep -c . <<<"$src_counts") tables"
    rm -f "$WORK/$db.counts.diff"
  else
    say "MISMATCH in $db row counts (source < > target): $WORK/$db.counts.diff"
    status=1
  fi
done
((status == 0)) || die "verification failed; keep the local stack as the source of truth"
if printf '%s\n' "${DB_LIST[@]}" | grep -qx aios; then
  applied=$(tq aios "/* aios:migrations */ SELECT version FROM schema_migrations ORDER BY 1" 2>/dev/null || true)
  for f in migrations/[0-9][0-9][0-9]_*.sql; do
    [[ -e "$f" ]] || continue
    v=$(basename "$f" .sql)
    grep -qx "$v" <<<"$applied" && continue
    say "applying migration $v"
    { echo "/* aios:migrate */ SET ROLE aios;"; cat "$f"; echo; echo "INSERT INTO schema_migrations(version) VALUES ('$v');"; } |
      with_pg T_ aios client psql -X -v ON_ERROR_STOP=1 --single-transaction -q || die "migration $v failed"
  done
  say "migrations up to date: $(tq aios "/* aios:migrations */ SELECT max(version) FROM schema_migrations")"
fi

for db in "${DB_LIST[@]}"; do tq "$db" "/* aios:analyze */ ANALYZE" >/dev/null; done
say "done. Dumps kept in $WORK (gitignored). Next: RUNBOOK §9.3 step 5 (files)."
