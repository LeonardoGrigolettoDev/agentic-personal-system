#!/usr/bin/env bash
# Copy data/storage (storage://<bucket>/<key>) to an S3-compatible bucket - Railway Bucket or Cloudflare R2 -
# for the cloud move (ARCHITECTURE §24.11, §25.4). The object layout matches the knowledge S3 backend:
# data/storage/<bucket>/<key> -> s3://$STORAGE_S3_BUCKET/<bucket>/<key>.
# DRY-RUN by default (rclone --dry-run: lists both sides, writes nothing); --apply transfers.
#
# rclone install: `curl -fsSL https://rclone.org/install.sh | sudo bash` (official script), or without sudo
#   download the linux-amd64 zip from https://rclone.org/downloads/ and put the `rclone` binary in ~/.local/bin.
# Remote "aios" comes from the environment only, never an rclone.conf:
#   RCLONE_CONFIG_AIOS_TYPE=s3 RCLONE_CONFIG_AIOS_PROVIDER=Other|Cloudflare RCLONE_CONFIG_AIOS_ENDPOINT=...
#   RCLONE_CONFIG_AIOS_ACCESS_KEY_ID=... RCLONE_CONFIG_AIOS_SECRET_ACCESS_KEY=... RCLONE_CONFIG_AIOS_REGION=auto
#   RCLONE_CONFIG_AIOS_FORCE_PATH_STYLE=false
# or, equivalently, the STORAGE_S3_* variables the services use (mapped below when RCLONE_* are unset).
# URL style: rclone's "Other" provider defaults to path-style requests, but Railway Buckets use virtual-hosted
# style, so FORCE_PATH_STYLE defaults to false here; an R2 endpoint keeps rclone's path style. A Railway bucket
# whose Credentials tab says "path" needs STORAGE_S3_FORCE_PATH_STYLE=true.
set -euo pipefail
cd "$(dirname "$0")/../.."

usage() {
  cat <<'EOF'
usage: storage-sync.sh [--apply] [--mirror] [--pull] [--src DIR] [--bucket NAME] [--exclude PATTERN]...
  default   copy new/changed files local -> remote (never deletes remote objects), dry run
  --apply   actually transfer
  --mirror  rclone sync: also DELETE remote objects missing locally (only before the cutover)
  --pull    remote -> local (rollback); never deletes local files
  --src     local root (default: data/storage)       --bucket  default: $STORAGE_S3_BUCKET
  --exclude rclone filter pattern, repeatable (e.g. 'env.bak' when copying backups/ off-site)
EOF
}

APPLY=0 MIRROR=0 PULL=0
SRC="${STORAGE_LOCAL_ROOT:-data/storage}"
BUCKET="${STORAGE_S3_BUCKET:-}"
REMOTE="${STORAGE_SYNC_REMOTE:-aios}"
EXCLUDES=('*.part' 'media/processing/**')
while (($#)); do
  case "$1" in
    --apply) APPLY=1 ;;
    --mirror) MIRROR=1 ;;
    --pull) PULL=1 ;;
    --src) SRC="${2:?--src needs a directory}"; shift ;;
    --bucket) BUCKET="${2:?--bucket needs a name}"; shift ;;
    --exclude) EXCLUDES+=("${2:?--exclude needs a pattern}"); shift ;;
    -h | --help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
((MIRROR && PULL)) && die "--mirror and --pull are exclusive"
[[ "$REMOTE" =~ ^[a-z][a-z0-9_]*$ ]] || die "STORAGE_SYNC_REMOTE must be a simple name"

command -v rclone >/dev/null || die "rclone not found: https://rclone.org/install/ (see the header of this script)"
[[ -n "$BUCKET" ]] || die "no bucket: set STORAGE_S3_BUCKET or pass --bucket (the Railway bucket's BUCKET variable)"
[[ "$BUCKET" =~ ^[a-z0-9][a-z0-9.-]{1,62}$ ]] || die "invalid bucket name: $BUCKET"

# rclone reads RCLONE_CONFIG_<REMOTE>_<OPTION>; fill them from STORAGE_S3_* when not given explicitly
up=$(tr '[:lower:]' '[:upper:]' <<<"$REMOTE")
cfg() { local v="RCLONE_CONFIG_${up}_$1"; [[ -n "${!v:-}" ]] || export "$v=$2"; }
cfg TYPE s3
cfg PROVIDER Other
[[ -n "${STORAGE_S3_ENDPOINT:-}" ]] && cfg ENDPOINT "$STORAGE_S3_ENDPOINT"
[[ -n "${STORAGE_S3_ACCESS_KEY_ID:-}" ]] && cfg ACCESS_KEY_ID "$STORAGE_S3_ACCESS_KEY_ID"
[[ -n "${STORAGE_S3_SECRET_ACCESS_KEY:-}" ]] && cfg SECRET_ACCESS_KEY "$STORAGE_S3_SECRET_ACCESS_KEY"
cfg REGION "${STORAGE_S3_REGION:-auto}"
endpoint_var="RCLONE_CONFIG_${up}_ENDPOINT"
path_style=false
[[ "${!endpoint_var:-}" == *.r2.cloudflarestorage.com* ]] && path_style=true
cfg FORCE_PATH_STYLE "${STORAGE_S3_FORCE_PATH_STYLE:-$path_style}"
fps="RCLONE_CONFIG_${up}_FORCE_PATH_STYLE"
[[ "${!fps}" =~ ^(true|false)$ ]] || die "$fps must be true or false (got '${!fps}')"
for opt in ENDPOINT ACCESS_KEY_ID SECRET_ACCESS_KEY; do
  v="RCLONE_CONFIG_${up}_$opt"
  [[ -n "${!v:-}" ]] || die "$v (or STORAGE_S3_$opt) is not set"
done

local_root="${SRC%/}"
remote_root="$REMOTE:$BUCKET"
if ((PULL)); then
  mkdir -p "$local_root"
  from="$remote_root" to="$local_root" verb=copy
else
  [[ -d "$local_root" ]] || die "$local_root does not exist"
  from="$local_root" to="$remote_root" verb=$( ((MIRROR)) && echo sync || echo copy)
fi

# .part = in-flight writes of the local backend; media/processing is scratch space cleared after each job
filters=()
for x in "${EXCLUDES[@]}"; do filters+=(--exclude "$x"); done
flags=("${filters[@]}" --fast-list --checkers 8 --transfers 4 --s3-no-check-bucket --stats-one-line --stats 30s -v)
((APPLY)) || flags+=(--dry-run)

echo "storage-sync: rclone $verb $from -> $to $( ((APPLY)) || echo '(DRY-RUN: nothing is written)')"
((MIRROR && APPLY)) && echo "WARNING: --mirror deletes remote objects that are not in $local_root"
rclone "$verb" "$from" "$to" "${flags[@]}"
if ((APPLY)); then
  rclone check "$from" "$to" --one-way --size-only "${filters[@]}" --s3-no-check-bucket ||
    die "post-transfer check found differences"
  echo "storage-sync: done and verified (size-only, one-way)"
fi
