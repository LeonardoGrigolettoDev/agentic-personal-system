#!/usr/bin/env bash
# Apply migrations/NNN_*.sql in order, each in its own transaction, tracked in schema_migrations.
set -euo pipefail
cd "$(dirname "$0")/../.."
PSQL=(docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U aios -d aios -qAt)

"${PSQL[@]}" -c "CREATE TABLE IF NOT EXISTS schema_migrations(version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
for f in migrations/[0-9][0-9][0-9]_*.sql; do
  v="$(basename "$f" .sql)"
  [[ -n "$("${PSQL[@]}" -c "SELECT 1 FROM schema_migrations WHERE version='$v'")" ]] && continue
  echo "applying $v"
  { cat "$f"; echo; echo "INSERT INTO schema_migrations(version) VALUES ('$v');"; } |
    docker compose exec -T postgres psql -v ON_ERROR_STOP=1 --single-transaction -q -U aios -d aios
done
echo "migrations up to date: $("${PSQL[@]}" -c 'SELECT max(version) FROM schema_migrations')"
