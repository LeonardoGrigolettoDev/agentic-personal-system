#!/usr/bin/env bash
# Restore drill (ARCHITECTURE §24.10): prove the newest local backup restores, before you need it and before the
# Railway cutover. Restores backups/<ts>/aios.dump into a scratch database (default aios_restore_test) on the local
# postgres, compares row counts with the live aios, checks pgvector works, checks the other backup artifacts,
# then drops the scratch database (KEEP=1 keeps it). Never touches the live databases.
#   infra/scripts/railway-restore-drill.sh [--db-only] [backups/<ts>]
# --db-only skips the hermes-data.tgz/env.bak checks (a dump downloaded from the Railway db-backup bucket).
set -euo pipefail
cd "$(dirname "$0")/../.."

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
DB="${RESTORE_DB:-aios_restore_test}"
[[ "$DB" =~ ^[a-z_][a-z0-9_]*_restore_test$ ]] || die "RESTORE_DB must end in _restore_test (got '$DB')"

DB_ONLY=0
if [[ "${1:-}" == --db-only ]]; then DB_ONLY=1; shift; fi
dir="${1:-}"
if [[ -z "$dir" ]]; then
  dir=$(find backups -mindepth 1 -maxdepth 1 -type d -name '[0-9]*T*Z' 2>/dev/null | sort | tail -n1)
  [[ -n "$dir" ]] || die "no backups/<timestamp>/ directory: run make backup first"
fi
dump="$dir/aios.dump"
[[ -s "$dump" ]] || die "$dump is missing or empty"
command -v docker >/dev/null || die "docker not found"
[[ -n "$(docker compose ps -q --status running postgres 2>/dev/null)" ]] || die "local postgres is not running (make up-core)"

PSQL=(docker compose exec -T postgres psql -X -v ON_ERROR_STOP=1 -U postgres -qAt)
in_container="/tmp/$DB.dump"
cleanup() {
  docker compose exec -T postgres rm -f "$in_container" >/dev/null 2>&1 || true
  if [[ "${KEEP:-0}" != 1 ]]; then
    "${PSQL[@]}" -d postgres -c "/* aios:drill-drop */ DROP DATABASE IF EXISTS \"$DB\" WITH (FORCE)" >/dev/null 2>&1 ||
      echo "WARN: could not drop $DB; drop it by hand" >&2
  fi
}
trap cleanup EXIT

echo "drill: $dump -> $DB ($(du -h "$dump" | cut -f1))"
"${PSQL[@]}" -d postgres -c "/* aios:drill-drop */ DROP DATABASE IF EXISTS \"$DB\" WITH (FORCE)" >/dev/null
"${PSQL[@]}" -d postgres -c "/* aios:drill-create */ CREATE DATABASE \"$DB\" OWNER aios" >/dev/null
docker compose cp "$dump" "postgres:$in_container" >/dev/null
toc_tables=$(docker compose exec -T postgres pg_restore -l "$in_container" | grep -c '^[0-9]*; [0-9]* [0-9]* TABLE ' || true)
start=$SECONDS
docker compose exec -T postgres pg_restore -U postgres -d "$DB" --exit-on-error --single-transaction "$in_container" ||
  die "pg_restore failed: this backup does NOT restore"
echo "restored in $((SECONDS - start))s ($toc_tables tables in the dump)"

COUNTS="/* aios:counts */ SELECT format('%I.%I', schemaname, tablename) || '|' ||
  (xpath('/row/c/text()', query_to_xml(format('SELECT count(*) AS c FROM %I.%I', schemaname, tablename),
   false, true, '')))[1]::text FROM pg_tables WHERE schemaname NOT IN ('pg_catalog', 'information_schema') ORDER BY 1"
restored=$("${PSQL[@]}" -d "$DB" -c "$COUNTS")
live=$("${PSQL[@]}" -d aios -c "$COUNTS")
printf '\n%-36s %12s %12s\n' table restored live
join -t'|' -a1 -e '-' -o '0,1.2,2.2' <(sort <<<"$restored") <(sort <<<"$live") |
  awk -F'|' '{printf "%-36s %12s %12s\n", $1, $2, $3}'

fail=0
verdict() { # label cmd... : one line per check, remember failures
  local label=$1; shift
  if "$@"; then echo "  ok    $label"; else echo "  FAIL  $label"; fail=1; fi
}
all_tables() { [[ "$toc_tables" -gt 0 && "$(grep -c . <<<"$restored")" -ge "$toc_tables" ]]; }
toc_readable() { docker compose exec -T postgres pg_restore -l <"$1" >/dev/null 2>&1; }
archive_ok() { [[ -s "$1" ]] && tar -tzf "$1" >/dev/null; }
version=$("${PSQL[@]}" -d "$DB" -c "/* aios:drill-version */ SELECT coalesce(max(version), '') FROM schema_migrations" 2>/dev/null || true)
vector=$("${PSQL[@]}" -d "$DB" -c "/* aios:drill-vector */ SELECT ('[1,2,3]'::vector <-> '[1,2,4]'::vector) = 1" 2>/dev/null || true)
echo
verdict "all $toc_tables tables restored" all_tables
verdict "schema_migrations at ${version:-?}" test -n "$version"
verdict "pgvector operators work" test "$vector" = t
for f in litellm.dump langfuse.dump; do
  if [[ -s "$dir/$f" ]]; then verdict "$f has a readable TOC" toc_readable "$dir/$f"; else echo "  skip  $f (not in this backup)"; fi
done
if ((DB_ONLY)); then
  echo "  skip  hermes-data.tgz, env.bak (--db-only)"
else
  verdict "hermes-data.tgz is a valid archive" archive_ok "$dir/hermes-data.tgz"
  verdict "env.bak present (holds LITELLM_SALT_KEY)" test -s "$dir/env.bak"
fi
((fail == 0)) || die "restore drill FAILED for $dir"
echo "restore drill passed for $dir$([[ "${KEEP:-0}" == 1 ]] && echo " ($DB kept)")"
