#!/usr/bin/env bash
# Railway cron job (ARCHITECTURE §24.10 in the cloud): pg_dump -Fc of each database in BACKUP_DATABASES, streamed
# to an rclone remote configured only through RCLONE_CONFIG_<REMOTE>_* variables, then prune dumps older than
# BACKUP_RETENTION_DAYS. Exits non-zero on any failure so the run shows up as failed in Railway.
set -euo pipefail

log() { printf '{"ts":"%s","level":"%s","component":"db-backup","msg":"%s"}\n' "$(date -u +%FT%TZ)" "$1" "$2"; }
die() { log error "$1"; exit 1; }

: "${PGHOST:?PGHOST is required}" "${PGPASSWORD:?PGPASSWORD is required}"
: "${BACKUP_REMOTE:?BACKUP_REMOTE is required, e.g. aios:<bucket>/postgres}"
export PGUSER="${PGUSER:-postgres}" PGPORT="${PGPORT:-5432}" PGCONNECT_TIMEOUT="${PGCONNECT_TIMEOUT:-15}"
dbs="${BACKUP_DATABASES:-aios,litellm}"
retention="${BACKUP_RETENTION_DAYS:-14}"
[[ "$retention" =~ ^[0-9]+$ && "$retention" -ge 1 ]] || die "BACKUP_RETENTION_DAYS must be a positive integer"
[[ "$BACKUP_REMOTE" == *:* ]] || die "BACKUP_REMOTE must be <remote>:<bucket>[/prefix]"

ts="$(date -u +%Y%m%dT%H%M%SZ)"
dest="${BACKUP_REMOTE%/}/$ts"
IFS=',' read -r -a list <<<"$dbs"
for db in "${list[@]}"; do
  db="${db// /}"
  [[ "$db" =~ ^[a-z_][a-z0-9_]*$ ]] || die "invalid database name: $db"
  start=$SECONDS
  if ! pg_dump -Fc --no-password -d "$db" | rclone rcat --s3-no-check-bucket "$dest/$db.dump"; then
    rclone deletefile --s3-no-check-bucket "$dest/$db.dump" 2>/dev/null || true
    die "dump of $db failed"
  fi
  size=$(rclone size --json --s3-no-check-bucket "$dest/$db.dump" | sed -E 's/.*"bytes":([0-9]+).*/\1/')
  [[ "${size:-0}" -gt 0 ]] || die "dump of $db is empty"
  log info "$db -> $dest/$db.dump ($size bytes, $((SECONDS - start))s)"
done

rclone delete --min-age "${retention}d" --s3-no-check-bucket "${BACKUP_REMOTE%/}" || die "pruning failed"
log info "done; dumps older than ${retention}d pruned"
