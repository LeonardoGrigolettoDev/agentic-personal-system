#!/usr/bin/env bash
# pg_dump every DB + Hermes state + repo config into backups/<ts>/ (§24.10). Keep 14 days locally.
# Copy backups/ to R2 / external disk too: the local DB must never be the only copy of memory.
# Never place this repo or backups/ under ~/Obsidian/* or any synced folder (.env holds secrets).
set -euo pipefail
cd "$(dirname "$0")/../.."
ts="$(date -u +%Y%m%dT%H%M%SZ)"
out="backups/$ts"
mkdir -p "$out"
chmod 700 backups "$out"

for db in aios litellm langfuse; do
  if docker compose exec -T postgres pg_dump -U postgres -Fc "$db" >"$out/$db.dump" 2>"$out/$db.err"; then
    rm -f "$out/$db.err"
  else
    echo "WARN: pg_dump $db failed: $(cat "$out/$db.err")" >&2
    rm -f "$out/$db.dump"
  fi
done

# Hermes state.db is SQLite WAL: take a consistent copy through the sqlite backup API when running
if [[ -n "$(docker compose --profile agent ps -q --status running hermes 2>/dev/null)" ]]; then
  docker compose exec -T hermes /opt/hermes/.venv/bin/python -c \
    "import sqlite3;s=sqlite3.connect('/opt/data/state.db');d=sqlite3.connect('/opt/data/state.backup.db');s.backup(d);d.close()" ||
    echo "WARN: hermes sqlite backup failed" >&2
fi
tar --exclude='data/hermes/state.db' --exclude='data/hermes/state.db-*' \
  -czf "$out/hermes-data.tgz" data/hermes config agents skills prompts workflows 2>/dev/null || true
install -m 600 .env "$out/env.bak" # holds LITELLM_SALT_KEY - losing it bricks stored LiteLLM keys

find backups -mindepth 1 -maxdepth 1 -type d -mtime +14 -exec rm -rf {} +
echo "backup -> $out ($(du -sh "$out" | cut -f1))"
