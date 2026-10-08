#!/usr/bin/env bash
# Tests for the Railway kit: infra/railway/**, infra/scripts/{railway-*,storage-sync,tailscale-*}.sh, mk/railway.mk.
# Hermetic: no Docker, network or Railway. docker/psql/pg_dump/pg_restore/rclone/tailscale/... are PATH shims
# (tests/shims/shim) that log their arguments, so every case can assert what would have been executed.
# Optional: RAILWAY_SDK_DIR=<dir with node_modules/{railway,typescript}> also type-checks the generated IaC.
# `check && ok || bad` is the assertion idiom here (ok always succeeds); ${{ }} in single quotes is test data.
# shellcheck disable=SC2015,SC2016
set -euo pipefail
cd "$(dirname "$0")/../../.."
REPO=$PWD
TMP=$(mktemp -d "${TMPDIR:-/tmp}/aios-railway-tests.XXXXXX")
trap 'rm -rf "$TMP"' EXIT
BIN=$TMP/bin
mkdir -p "$BIN"
for t in docker psql pg_dump pg_restore rclone tailscale tailscaled socat curl; do
  ln -s "$REPO/infra/railway/tests/shims/shim" "$BIN/$t"
done
export SHIM_REPO=$REPO PYTHONDONTWRITEBYTECODE=1 PYTHONPYCACHEPREFIX="$TMP/pycache"
PASS=0 FAIL=0
OUT=$TMP/out

ok() { PASS=$((PASS + 1)); printf '  \033[32mok\033[0m   %s\n' "$1"; }
bad() { FAIL=$((FAIL + 1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; [[ ! -s "$OUT" ]] || sed 's/^/       | /' "$OUT" | tail -n 25; }
section() { printf '\n%s\n' "$1"; }

# run <expected-exit> cmd... : fresh shim log, shims first on PATH, stdin closed; output in $OUT
run() {
  local want=$1 got=0
  shift
  export SHIM_LOG=$TMP/shim.log
  : >"$SHIM_LOG"
  : >"$SHIM_LOG.stdin"
  : >"$SHIM_LOG.env"
  PATH="$BIN:$PATH" "$@" </dev/null >"$OUT" 2>&1 || got=$?
  [[ "$got" == "$want" ]] || { echo "(exit $got, expected $want)" >>"$OUT"; return 1; }
}
has() { grep -Eq -- "$1" "$2"; }
free_port() { python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])'; }
http_status() { # url -> HTTP status, waiting up to 5 s for a listener (000 if none); curl is a shim here
  python3 - "$1" <<'PY'
import sys, time, urllib.error, urllib.request
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
for _ in range(50):
    try:
        print(opener.open(sys.argv[1], timeout=1).status)
        sys.exit(0)
    except urllib.error.HTTPError as e:
        print(e.code)
        sys.exit(0)
    except OSError:
        time.sleep(0.1)
print("000")
PY
}
case_() { # name expected-exit cmd... -- then checks are done by the caller via && chains
  local name=$1
  shift
  if run "$@"; then return 0; else bad "$name"; return 1; fi
}
writes_in_log() { grep -E 'pg_dump|pg_restore|CREATE|INSERT|ANALYZE|aios:create|aios:migrate|aios:extensions-create|compose cp' \
  "$SHIM_LOG" "$SHIM_LOG.stdin" | grep -v 'pg_restore -l' || true; }

# ---------------------------------------------------------------------------------------------------------
section "syntax"
mapfile -t BASH_SCRIPTS < <(ls infra/scripts/railway-*.sh infra/scripts/storage-sync.sh infra/scripts/tailscale-*.sh \
  infra/railway/db-backup/backup.sh infra/railway/tests/run.sh infra/railway/tests/integration.sh \
  infra/railway/tests/initdb-emulate.sh infra/railway/tests/shims/shim)
mapfile -t SH_SCRIPTS < <(ls infra/railway/litellm/entrypoint.sh infra/railway/edge-gw/entrypoint.sh infra/railway/hermes/start.sh)
SOURCED=(infra/railway/lib/maintenance.sh infra/railway/postgres/initdb/00-local-socket.sh)
for f in "${BASH_SCRIPTS[@]}"; do if bash -n "$f" 2>"$OUT"; then ok "bash -n $f"; else bad "bash -n $f"; fi; done
for f in "${SH_SCRIPTS[@]}" "${SOURCED[@]}"; do if sh -n "$f" 2>"$OUT"; then ok "sh -n $f"; else bad "sh -n $f"; fi; done
for f in "${BASH_SCRIPTS[@]}" "${SH_SCRIPTS[@]}"; do
  [[ -x "$f" ]] && ok "executable $f" || { : >"$OUT"; bad "executable $f"; }
done
SHELLCHECK=()
if command -v shellcheck >/dev/null; then
  SHELLCHECK=(shellcheck)
elif command -v uvx >/dev/null && uvx --offline --from shellcheck-py shellcheck --version >/dev/null 2>&1; then
  SHELLCHECK=(uvx --offline --from shellcheck-py shellcheck)
fi
if ((${#SHELLCHECK[@]})); then
  if "${SHELLCHECK[@]}" -x "${BASH_SCRIPTS[@]}" "${SH_SCRIPTS[@]}" "${SOURCED[@]}" >"$OUT" 2>&1; then ok "shellcheck"; else bad "shellcheck"; fi
else
  echo "  skip shellcheck (not installed)"
fi
if python3 -m py_compile infra/railway/lib/manifest.py infra/railway/hermes/rehost.py infra/railway/hermes/hermes_state.py \
  infra/railway/tests/validate_railway_json.py infra/railway/tests/test_parity.py 2>"$OUT"; then
  ok "python compiles"
else bad "python compiles"; fi

# ---------------------------------------------------------------------------------------------------------
section "railway.json + manifest"
if python3 infra/railway/tests/validate_railway_json.py >"$OUT" 2>&1; then
  ok "every railway.json is valid JSON and matches the Railway schema ($(grep -c '^ok' "$OUT") files)"
else bad "railway.json schema"; fi
if run 0 bash infra/scripts/railway-plan.sh --check; then ok "manifest check (references, secrets, Dockerfiles)"; else bad "manifest check"; fi

printf 'PG_SUPERUSER_PASSWORD=supersecret-pg-xyz\nDECISION_BACKENDS=rules,local,jev\nDECISION_API_KEY=supersecret-dk\n' >"$TMP/env"
if case_ "plan" 0 bash infra/scripts/railway-plan.sh --env-file "$TMP/env"; then
  missing=""
  for s in postgres litellm decision knowledge sandbox hermes edge-gw db-backup redis aios-storage aios-backups; do
    has "^  $s " "$OUT" || missing+=" $s"
  done
  [[ -z "$missing" ]] && ok "plan lists every service and resource" || bad "plan misses:$missing"
  has supersecret "$OUT" && bad "plan printed a secret value" || ok "plan never prints secret values"
  has 'decision.DECISION_BACKENDS: .env=rules,local,jev' "$OUT" && ok "plan reports .env drift" || bad "plan drift"
  has '^  secret PG_SUPERUSER_PASSWORD +\.env: set' "$OUT" && ok "plan shows .env status of shared secrets" || bad "plan .env status"
fi

if case_ "iac" 0 bash infra/scripts/railway-plan.sh --iac --repo example/agent-system; then
  cp "$OUT" "$TMP/railway.ts"
  has 'from "railway/iac"' "$TMP/railway.ts" && has 'service\("hermes"' "$TMP/railway.ts" &&
    has '"/opt/data": volHermesData' "$TMP/railway.ts" && has 'cronSchedule' "$TMP/railway.ts" &&
    ok "IaC (.railway/railway.ts) generated" || bad "IaC content"
  if [[ -n "${RAILWAY_SDK_DIR:-}" ]]; then
    mkdir -p "$RAILWAY_SDK_DIR/.railway" && cp "$TMP/railway.ts" "$RAILWAY_SDK_DIR/.railway/railway.ts"
    if (cd "$RAILWAY_SDK_DIR" && npx --no-install tsc --noEmit --strict --module nodenext --moduleResolution nodenext \
      --target es2022 .railway/railway.ts) >"$OUT" 2>&1; then ok "IaC type-checks against the railway SDK"; else bad "IaC tsc"; fi
  else
    echo "  skip IaC type-check (set RAILWAY_SDK_DIR)"
  fi
fi

cp -r infra/railway "$TMP/rw"
echo 'DECISION_API_KEY=plaintext-key' >>"$TMP/rw/decision/variables.env"
sed -i 's/^DECISION_API_KEY=\${{shared.DECISION_API_KEY}}$//' "$TMP/rw/decision/variables.env"
echo 'BROKEN=${{nosuch.VAR}}' >>"$TMP/rw/hermes/variables.env"
echo 'AIOS_URL=postgresql://aios:hunter2@x:5432/aios' >>"$TMP/rw/knowledge/variables.env"
echo 'PGHOST=${{RAILWAY_PRIVATE_DOMAIN}}' >>"$TMP/rw/postgres/variables.env"
if run 1 env AIOS_RAILWAY_DIR="$TMP/rw" python3 infra/railway/lib/manifest.py check; then
  has 'DECISION_API_KEY holds a literal' "$OUT" && has "unknown resource 'nosuch'" "$OUT" &&
    has 'embeds a literal password' "$OUT" && ok "manifest rejects literal secrets and dangling references" ||
    bad "manifest negative checks"
  has 'postgres: never set PGHOST/PGHOSTADDR' "$OUT" && ok "manifest rejects a PGHOST on the postgres service itself" ||
    bad "manifest PGHOST check"
else bad "manifest accepted a broken copy"; fi

if python3 -c 'import yaml' 2>/dev/null; then
  if python3 infra/railway/tests/test_parity.py >"$OUT" 2>&1; then ok "$(grep '^ok' "$OUT" | sed 's/^ok *//')"; else bad "compose parity / pins"; fi
else
  echo "  skip compose parity (python3 has no PyYAML)"
fi

# ---------------------------------------------------------------------------------------------------------
section "postgres first boot"
grep -qxF 'COPY --chmod=0644 infra/railway/postgres/initdb/00-local-socket.sh /docker-entrypoint-initdb.d/00-local-socket.sh' \
  infra/railway/postgres/Dockerfile && ok "image installs the socket guard 0644 (sourced) ahead of 01-init.sh" ||
  { : >"$OUT"; bad "socket guard COPY"; }
grep -Eq '^PGHOST(ADDR)?=' infra/railway/postgres/variables.env && { : >"$OUT"; bad "postgres/variables.env sets PGHOST"; } ||
  ok "the postgres service sets no PGHOST/PGHOSTADDR"
PG=$TMP/pg
mkdir -p "$PG/bin" "$PG/initdb" "$PG/bare"
printf '#!/bin/sh\necho "PGHOST=${PGHOST-unset} PGHOSTADDR=${PGHOSTADDR-unset}" >>"%s/calls"\ncat >/dev/null\n' "$PG" >"$PG/bin/psql"
chmod +x "$PG/bin/psql"
cp -p infra/docker/postgres/initdb/* "$PG/initdb/"
cp -p infra/docker/postgres/initdb/* "$PG/bare/"
install -m 0644 infra/railway/postgres/initdb/00-local-socket.sh "$PG/initdb/"
PGENV=(POSTGRES_USER=postgres AIOS_DB_PASSWORD=a AIOS_READER_PASSWORD=b LITELLM_DB_PASSWORD=c LANGFUSE_DB_PASSWORD=d
  PGHOST=postgres.railway.internal PGHOSTADDR=fd12::2 PATH="$PG/bin:$PATH")
: >"$PG/calls"
if case_ "initdb guard" 0 env "${PGENV[@]}" bash infra/railway/tests/initdb-emulate.sh "$PG/initdb"; then
  [[ "$(sort -u "$PG/calls")" == "PGHOST=unset PGHOSTADDR=unset" && $(wc -l <"$PG/calls") -ge 2 ]] &&
    has 'sourcing .*00-local-socket.sh' "$OUT" &&
    ok "init scripts reach psql without PGHOST/PGHOSTADDR (the first-boot server's Unix socket)" || bad "initdb PGHOST guard"
fi
: >"$PG/calls"
if case_ "initdb control" 0 env "${PGENV[@]}" bash infra/railway/tests/initdb-emulate.sh "$PG/bare"; then
  has 'PGHOST=postgres.railway.internal' "$PG/calls" &&
    ok "control: without the guard the init scripts follow PGHOST over TCP" || bad "initdb control"
fi

# ---------------------------------------------------------------------------------------------------------
section "railway-db-migrate.sh"
M=infra/scripts/railway-db-migrate.sh
TGT="postgresql://postgres:tgt-secret-pw@127.0.0.1:15432/postgres"
unset TARGET_DATABASE_URL
if run 1 bash $M; then has 'no target' "$OUT" && ok "requires a target" || bad "no-target message"; else bad "no target"; fi
if case_ "dry run" 0 bash $M --target "$TGT"; then
  has 'DRY-RUN' "$OUT" && [[ -z "$(writes_in_log)" ]] && ok "dry run (default) performs no writes" ||
    { bad "dry run wrote: $(writes_in_log | head -3)"; }
  has 'pg_available_extensions' "$SHIM_LOG" && ok "dry run checks pgvector on the target first" || bad "no extension check"
  has 'tgt-secret-pw' "$SHIM_LOG" && bad "target password leaked into argv" || ok "target password never in argv"
fi
if run 1 env SHIM_NO_VECTOR=1 bash $M --target "$TGT"; then
  has "extension 'vector' is not available" "$OUT" && [[ -z "$(writes_in_log)" ]] && ok "refuses a target without pgvector" ||
    bad "pgvector message"
else bad "pgvector missing not refused"; fi
if run 1 bash $M --target "postgresql://postgres:pw@roundhouse.proxy.rlwy.net:11105/railway"; then
  has 'tunnel-only' "$OUT" && ok "refuses a remote target without sslmode" || bad "insecure message"
else bad "insecure target accepted"; fi
if run 0 bash $M --target "postgresql://postgres:pw@roundhouse.proxy.rlwy.net:11105/railway?sslmode=require"; then
  ok "accepts a remote target with sslmode=require"
else bad "sslmode=require target"; fi
if run 1 env SHIM_TGT_TABLES=7 bash $M --target "$TGT"; then
  has 'refusing to overwrite' "$OUT" && ok "refuses a non-empty target database" || bad "non-empty message"
else bad "non-empty target accepted"; fi
if run 1 env SHIM_TGT_DBS='litellm' SHIM_TGT_TABLES=58 bash $M --target "$TGT"; then
  has "'litellm' already has 58 tables: the cloud LiteLLM booted before the copy" "$OUT" && has 'AIOS_MAINTENANCE' "$OUT" &&
    ok "a migrated cloud litellm DB is refused with the recovery steps" || bad "litellm non-empty message"
else bad "migrated cloud litellm accepted"; fi
if case_ "sessions dry" 0 env SHIM_TGT_SESSIONS='aios decision fd12::5\nlitellm litellm-proxy fd12::7' bash $M --target "$TGT"; then
  has 'WARNING: other sessions on the target' "$OUT" && has 'litellm litellm-proxy fd12::7' "$OUT" &&
    ok "dry run lists cloud sessions on the target databases" || bad "sessions warning"
fi
if run 1 env SHIM_TGT_SESSIONS='litellm litellm-proxy fd12::7' bash $M --target "$TGT" --apply --yes; then
  has 'cloud sessions are connected' "$OUT" && [[ -z "$(writes_in_log)" ]] &&
    ok "--apply refuses while cloud services are connected to the target" || bad "sessions refusal"
else bad "--apply ran with cloud sessions connected"; fi
if run 1 env SHIM_TGT_VERSION=160004 bash $M --target "$TGT"; then
  has 'older than source' "$OUT" && ok "refuses an older target major" || bad "older major message"
else bad "older target accepted"; fi
if run 1 bash $M --target "$TGT" --apply; then
  has 'no terminal' "$OUT" && [[ -z "$(writes_in_log)" ]] && ok "--apply without --yes needs a terminal confirmation" ||
    bad "--apply w/o --yes wrote"
else bad "--apply without --yes did not abort"; fi
if run 1 env SHIM_RUNNING='hermes\ndecision' bash $M --target "$TGT" --apply --yes; then
  has 'stop the writers' "$OUT" && [[ -z "$(writes_in_log)" ]] && ok "--apply refuses while app containers write" ||
    bad "writers message"
else bad "writers running not refused"; fi
mkdir -p "$TMP/work"
if case_ "apply" 0 env MIGRATE_WORK_DIR="$TMP/work" AIOS_DB_PASSWORD=role-secret-1 AIOS_READER_PASSWORD=role-secret-2 \
  LITELLM_DB_PASSWORD=role-secret-3 bash $M --target "$TGT" --apply --yes; then
  has 'compose exec -T postgres pg_dump -U postgres -Fc aios' "$SHIM_LOG" &&
    has 'compose exec -T postgres pg_dump -U postgres -Fc litellm' "$SHIM_LOG" && ok "--apply dumps aios and litellm" ||
    bad "--apply dumps"
  has 'pg_restore --no-owner --role=aios --single-transaction --exit-on-error -L /work/aios.toc' "$SHIM_LOG" &&
    has 'pg_restore --no-owner --role=litellm' "$SHIM_LOG" && ok "--apply restores as the owner roles" || bad "--apply restore"
  toc=$(find "$TMP/work" -name aios.toc | head -n1)
  [[ -n "$toc" ]] && ! has 'EXTENSION' "$toc" && has 'TABLE public documents' "$toc" &&
    ok "restore TOC drops extension entries, keeps tables" || bad "TOC filtering"
  has 'aios:create-role' "$SHIM_LOG.stdin" && has 'aios:counts' "$SHIM_LOG" && has 'verified aios' "$OUT" &&
    ok "--apply creates roles and verifies row counts" || bad "--apply roles/verify"
  grep -q 'role-secret' "$SHIM_LOG" "$SHIM_LOG.stdin" && bad "role password leaked" || ok "role passwords never in argv or SQL text"
fi
if case_ "host client" 0 bash $M --target "$TGT" --client host; then
  has '^psql	-X' "$SHIM_LOG" && ! has 'docker	run' "$SHIM_LOG" && ok "--client host uses local psql" || bad "--client host"
fi
if case_ "dump-only" 0 env MIGRATE_WORK_DIR="$TMP/work2" bash $M --dump-only; then
  [[ -n "$(find "$TMP/work2" -name aios.dump -size +0)" ]] && ! has 'pg_restore|docker	run' "$SHIM_LOG" &&
    has 'railway volume files --volume pgdata upload .*/aios.dump /aios.dump' "$OUT" &&
    has 'pg_restore -U postgres -d litellm' "$OUT" && ! has 'files upload .* --service' "$OUT" &&
    ok "--dump-only writes dumps and prints plan B (per database, volume chosen with --volume)" || bad "--dump-only"
fi

if [[ -n "${RAILWAY_IT_PGBIN:-}" && -n "${RAILWAY_IT_SOURCE_URL:-}" ]]; then
  if bash infra/railway/tests/integration.sh >"$OUT" 2>&1; then ok "$(tail -n1 "$OUT" | sed 's/^ok *//')"; else bad "integration"; fi
else
  echo "  skip real-PostgreSQL integration (set RAILWAY_IT_PGBIN and RAILWAY_IT_SOURCE_URL)"
fi

# ---------------------------------------------------------------------------------------------------------
section "storage-sync.sh"
S=infra/scripts/storage-sync.sh
mkdir -p "$TMP/storage/media/input"
S3ENV=(STORAGE_S3_BUCKET=aios-storage-x1 STORAGE_S3_ENDPOINT=https://t3.storageapi.dev
  STORAGE_S3_ACCESS_KEY_ID=AKIDTEST STORAGE_S3_SECRET_ACCESS_KEY=s3-secret-value)
if run 1 bash $S --src "$TMP/storage"; then has 'no bucket' "$OUT" && ok "requires a bucket" || bad "bucket message"; else bad "no bucket"; fi
if case_ "storage dry" 0 env "${S3ENV[@]}" bash $S --src "$TMP/storage"; then
  has "^rclone	copy $TMP/storage aios:aios-storage-x1 .*--dry-run" "$SHIM_LOG" && ! has 'rclone	check' "$SHIM_LOG" &&
    ok "dry run (default): rclone copy --dry-run, never deletes" || bad "storage dry run"
  has 's3-secret-value' "$SHIM_LOG" && bad "S3 secret in argv" || ok "S3 credentials only via RCLONE_CONFIG_* env"
  has '^RCLONE_CONFIG_AIOS_FORCE_PATH_STYLE=false$' "$SHIM_LOG.env" && has '^RCLONE_CONFIG_AIOS_PROVIDER=Other$' "$SHIM_LOG.env" &&
    ok "virtual-hosted-style requests for Railway Buckets (FORCE_PATH_STYLE=false)" || bad "storage URL style"
fi
if case_ "storage r2" 0 env "${S3ENV[@]}" STORAGE_S3_ENDPOINT=https://0123abcd.r2.cloudflarestorage.com bash $S --src "$TMP/storage"; then
  has '^RCLONE_CONFIG_AIOS_FORCE_PATH_STYLE=true$' "$SHIM_LOG.env" && ok "an R2 endpoint keeps rclone's path style" || bad "R2 style"
fi
if case_ "storage path override" 0 env "${S3ENV[@]}" STORAGE_S3_FORCE_PATH_STYLE=true bash $S --src "$TMP/storage"; then
  has '^RCLONE_CONFIG_AIOS_FORCE_PATH_STYLE=true$' "$SHIM_LOG.env" && ok "STORAGE_S3_FORCE_PATH_STYLE=true for a path-style bucket" ||
    bad "path-style override"
fi
if run 1 env "${S3ENV[@]}" STORAGE_S3_FORCE_PATH_STYLE=yes bash $S --src "$TMP/storage"; then
  ok "rejects a FORCE_PATH_STYLE that is not true/false"
else bad "bad FORCE_PATH_STYLE accepted"; fi
if case_ "storage apply" 0 env "${S3ENV[@]}" bash $S --src "$TMP/storage" --apply; then
  has "^rclone	copy $TMP/storage aios:aios-storage-x1" "$SHIM_LOG" && ! has 'dry-run' "$SHIM_LOG" &&
    has '^rclone	check' "$SHIM_LOG" && ok "--apply copies and verifies" || bad "storage apply"
fi
if case_ "storage mirror" 0 env "${S3ENV[@]}" bash $S --src "$TMP/storage" --mirror; then
  has '^rclone	sync .*--dry-run' "$SHIM_LOG" && ok "--mirror uses rclone sync (still dry without --apply)" || bad "mirror"
fi
if case_ "storage exclude" 0 env "${S3ENV[@]}" bash $S --src "$TMP/storage" --exclude env.bak; then
  has "--exclude env.bak" "$SHIM_LOG" && ok "--exclude adds rclone filters" || bad "exclude"
fi
if case_ "storage pull" 0 env "${S3ENV[@]}" bash $S --src "$TMP/storage" --pull --apply; then
  has "^rclone	copy aios:aios-storage-x1 $TMP/storage" "$SHIM_LOG" && ok "--pull copies bucket -> local (rollback)" || bad "pull"
fi

# ---------------------------------------------------------------------------------------------------------
section "tailscale-edge.sh"
T=infra/scripts/tailscale-edge.sh
if case_ "acl" 0 bash $T --acl; then
  has '"tag:aios-cloud"' "$OUT" && has '"tcp:8093", "tcp:11434"' "$OUT" && ! [[ -s "$SHIM_LOG" ]] &&
    ok "--acl prints the policy (tag:aios-cloud -> tag:thinkpad:8093,11434) offline" || bad "acl"
fi
if case_ "ts dry" 0 bash $T; then
  has 'DRY-RUN' "$OUT" && ! has 'serve --bg|serve --tcp|funnel' "$SHIM_LOG" &&
    has 'EDGE_GW_FORWARD=8093=thinkpad:8093,11434=thinkpad:11434' "$OUT" && ok "dry run (default) changes nothing" ||
    bad "tailscale dry run"
fi
if run 1 env SHIM_TS_SERVE_JSON='{"AllowFunnel":{"thinkpad.tail0000.ts.net:443":true}}' bash $T --apply; then
  has 'Funnel is ON' "$OUT" && ! has 'serve --bg' "$SHIM_LOG" && ok "refuses to continue while Funnel is on" || bad "funnel"
else bad "funnel not refused"; fi
if run 1 env SHIM_TS_STATE=NeedsLogin bash $T; then ok "requires a logged-in tailscale"; else bad "logged-out"; fi
if case_ "ts apply" 0 bash $T --apply; then
  has '^tailscale	serve --bg --yes --tcp 8093 tcp://127.0.0.1:8093' "$SHIM_LOG" &&
    has '^tailscale	serve --bg --yes --tcp 11434 tcp://127.0.0.1:11434' "$SHIM_LOG" && ! has 'funnel' "$SHIM_LOG" &&
    ok "--apply serves 8093 and 11434 tailnet-only (no funnel)" || bad "tailscale apply"
fi
if case_ "ts off" 0 bash $T --apply --off; then
  has '^tailscale	serve --tcp=8093 tcp://127.0.0.1:8093 off' "$SHIM_LOG" && ok "--apply --off removes the serve entries" || bad "off"
fi
if run 1 bash $T --ports 8093,99999; then ok "rejects invalid ports"; else bad "invalid port"; fi

# ---------------------------------------------------------------------------------------------------------
section "railway-restore-drill.sh"
D=infra/scripts/railway-restore-drill.sh
B=$TMP/backups/20261007T030000Z
mkdir -p "$B" "$TMP/tarsrc/data"
echo PGDMP >"$B/aios.dump"
echo PGDMP >"$B/litellm.dump"
echo 'LITELLM_SALT_KEY=x' >"$B/env.bak"
tar -czf "$B/hermes-data.tgz" -C "$TMP/tarsrc" data
if case_ "drill" 0 bash $D "$B"; then
  drops=$(grep -oE '(DROP|CREATE) DATABASE (IF EXISTS )?"[a-z_]*"' "$SHIM_LOG" | sort -u || true)
  [[ "$drops" == $'CREATE DATABASE "aios_restore_test"\nDROP DATABASE IF EXISTS "aios_restore_test"' ]] &&
    ok "drill only creates/drops aios_restore_test" || bad "drill touched another database: $drops"
  has 'restore drill passed' "$OUT" && has 'pg_restore -U postgres -d aios_restore_test' "$SHIM_LOG" &&
    ok "drill restores, checks and reports" || bad "drill output"
fi
mkdir -p "$TMP/cloud"
echo PGDMP >"$TMP/cloud/aios.dump"
if case_ "drill db-only" 0 bash $D --db-only "$TMP/cloud"; then
  has 'skip  hermes-data.tgz' "$OUT" && has 'restore drill passed' "$OUT" && ok "--db-only drills a bare dump (cloud backup)" ||
    bad "--db-only"
fi
if run 1 env RESTORE_DB=aios bash $D "$B"; then ok "refuses a scratch name that is not *_restore_test"; else bad "RESTORE_DB guard"; fi
if run 1 bash $D "$TMP/backups/none"; then ok "fails clearly without a dump"; else bad "missing dump"; fi

# ---------------------------------------------------------------------------------------------------------
section "service entrypoints"
L=$TMP/litellm-root
mkdir -p "$L/config/litellm" "$L/infra/scripts"
cp config/litellm/config.template.yaml "$L/config/litellm/"
cp infra/scripts/render-litellm-config.py "$L/infra/scripts/"
if python3 -c 'import yaml' 2>/dev/null; then
  if case_ "litellm entrypoint" 0 env -i PATH="/usr/bin:/bin" AIOS_ROOT="$L" AIOS_RAILWAY_LIB="$REPO/infra/railway/lib" \
    ANTHROPIC_API_KEY=sk-ant-secret-1 LITELLM_ENTRYPOINT=/bin/echo sh infra/railway/litellm/entrypoint.sh --port=4000; then
    cfg="$L/config/litellm/config.yaml"
    has 'anthropic/claude-sonnet' "$cfg" && ! has 'openrouter/|moonshot/|deepseek/' "$cfg" && ! has 'sk-ant-secret' "$cfg" &&
      [[ ! -e "$L/.env" ]] && has '^--port=4000$' "$OUT" &&
      ok "litellm: renders only keyed providers, no secrets on disk, execs upstream with CMD args" || bad "litellm render"
  fi
else
  echo "  skip litellm entrypoint (python3 has no PyYAML)"
fi
rm -f "$L/config/litellm/config.yaml"
port=$(free_port)
env -i PATH="/usr/bin:/bin" AIOS_ROOT="$L" AIOS_RAILWAY_LIB="$REPO/infra/railway/lib" AIOS_MAINTENANCE=1 PORT="$port" \
  LITELLM_ENTRYPOINT=/bin/echo sh infra/railway/litellm/entrypoint.sh --port=4000 >"$TMP/lm.out" 2>&1 &
lm_pid=$!
st_ok=$(http_status "http://127.0.0.1:$port/health/liveliness")
st_other=$(http_status "http://127.0.0.1:$port/v1/models")
{ kill "$lm_pid" && wait "$lm_pid"; } 2>/dev/null || true
cp "$TMP/lm.out" "$OUT"
[[ "$st_ok" == 200 && "$st_other" == 404 && ! -e "$L/config/litellm/config.yaml" ]] && ! has '--port=4000' "$OUT" &&
  has 'AIOS_MAINTENANCE=1' "$OUT" && ok "litellm: AIOS_MAINTENANCE=1 answers the health check only; no render, no proxy, no DB" ||
  bad "litellm maintenance ($st_ok/$st_other)"
for f in litellm hermes; do
  grep -qxF 'AIOS_MAINTENANCE=${{shared.AIOS_MAINTENANCE}}' "infra/railway/$f/variables.env" || { : >"$OUT"; bad "$f lacks AIOS_MAINTENANCE"; }
done
has '^AIOS_MAINTENANCE=1 ' infra/railway/shared.env && ok "litellm and hermes follow the shared AIOS_MAINTENANCE (default 1)" ||
  { : >"$OUT"; bad "shared AIOS_MAINTENANCE"; }

H=$TMP/hermes
mkdir -p "$H/lib" "$H/home"
cp infra/railway/lib/maintenance.sh infra/railway/hermes/hermes_state.py "$H/lib/"
cat >"$H/python" <<EOF
#!/bin/sh
# fake venv python: logs setup.py runs (exit \$SETUP_EXIT); the health server and hermes_state.py run for real
case "\$1" in */setup.py) echo "setup \$*" >>"$H/calls"; exit "\${SETUP_EXIT:-0}" ;; esac
exec python3 "\$@"
EOF
printf '#!/bin/sh\necho "hermes $* key=${AIOS_SANDBOX_SSH_KEY_B64:-unset}" >>"%s/calls"\n' "$H" >"$H/hermes"
chmod +x "$H/python" "$H/hermes"
HENV=(AIOS_RAILWAY_LIB="$H/lib" AIOS_PYTHON="$H/python" HERMES_BIN="$H/hermes" TERMINAL_SSH_KEY="$H/id_ed25519"
  AIOS_SETUP=/opt/aios/bin/setup.py)
key_b64=$(printf 'PRIVATE-KEY-MATERIAL' | base64 -w0)
: >"$H/calls"
if case_ "hermes start" 0 env "${HENV[@]}" HERMES_HOME="$H/home" AIOS_SANDBOX_SSH_KEY_B64="$key_b64" sh infra/railway/hermes/start.sh; then
  [[ "$(cat "$H/id_ed25519")" == PRIVATE-KEY-MATERIAL && "$(stat -c %a "$H/id_ed25519")" == 600 ]] &&
    has '^setup /opt/aios/bin/setup.py$' "$H/calls" && has '^hermes gateway run key=unset$' "$H/calls" &&
    has '^[0-9]{4}-[0-9]{2}-[0-9]{2}T' "$H/home/.aios-setup-ok" &&
    ok "hermes: key installed 0600, setup re-applied (marker written), gateway started without the key in its env" ||
    bad "hermes start"
fi

# the image's stage2 hook seeds Hermes' stock config.yaml before start.sh runs: that must not count as configured
mkdir -p "$H/fresh" && printf 'model:\n  default: anthropic/claude-opus-4\nterminal:\n  backend: local\n' >"$H/fresh/config.yaml"
: >"$H/calls"
if run 1 env "${HENV[@]}" HERMES_HOME="$H/fresh" SETUP_EXIT=3 sh infra/railway/hermes/start.sh; then
  has 'refusing to start a gateway on the stock config' "$OUT" && ! has '^hermes' "$H/calls" &&
    ok "hermes: failed setup on a volume never set up is fatal, despite the seeded stock config.yaml" || bad "hermes first boot"
else bad "hermes started on the stock config"; fi
: >"$H/calls"
if case_ "hermes setup fails, applied before" 0 env "${HENV[@]}" HERMES_HOME="$H/home" SETUP_EXIT=3 sh infra/railway/hermes/start.sh; then
  has '^hermes gateway run' "$H/calls" && has 'last applied AIOS config' "$OUT" &&
    ok "hermes: a failed re-apply keeps the gateway up on the last applied AIOS config" || bad "hermes degraded start"
fi

port=$(free_port)
: >"$H/calls"
env "${HENV[@]}" HERMES_HOME="$H/home" AIOS_MAINTENANCE=1 PORT="$port" sh infra/railway/hermes/start.sh >"$TMP/hm.out" 2>&1 &
hm_pid=$!
st_ok=$(http_status "http://127.0.0.1:$port/health")
st_other=$(http_status "http://127.0.0.1:$port/v1/models")
{ kill "$hm_pid" && wait "$hm_pid"; } 2>/dev/null || true
cp "$TMP/hm.out" "$OUT"
[[ "$st_ok" == 200 && "$st_other" == 404 && ! -s "$H/calls" ]] && has 'AIOS_MAINTENANCE=1' "$OUT" &&
  ok "hermes: AIOS_MAINTENANCE=1 answers /health only; no setup, no gateway (container stays up for ssh/volume files)" ||
  bad "hermes maintenance ($st_ok/$st_other)"

# State move: export from an instance that died with committed rows still in its -wal files; import (through
# start.sh, as at the cutover) into a volume that holds another instance's state.db plus its own -wal/-shm.
SRC=$TMP/state-src DST=$H/home
mkdir -p "$SRC/profiles/coder" "$SRC/sessions" "$SRC/plugins" "$SRC/.pre-import-old"
crash_db() { # path prefix rows: committed rows left only in <path>-wal (no close, no checkpoint)
  python3 - "$@" <<'PY'
import os, sqlite3, sys
path, prefix, rows = sys.argv[1], sys.argv[2], int(sys.argv[3])
con = sqlite3.connect(path, isolation_level=None)
con.execute("PRAGMA journal_mode=wal")
con.execute("PRAGMA wal_autocheckpoint=0")
con.execute("CREATE TABLE t(v TEXT)")
con.executemany("INSERT INTO t VALUES (?)", [(f"{prefix}-{i}",) for i in range(rows)])
os._exit(0)
PY
}
crash_db "$SRC/state.db" local 5
crash_db "$SRC/profiles/coder/state.db" coder 2
echo '{"id": 1}' >"$SRC/sessions/s1.json"
ln -s /opt/aios/plugins/aios "$SRC/plugins/aios"
echo stale >"$SRC/config.yaml.aios-bak"
echo stale >"$SRC/.aios-setup-ok"
echo stale >"$SRC/.pre-import-old/state.db"
db_rows() { python3 -c 'import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); print(c.execute("PRAGMA quick_check").fetchone()[0], ",".join(r[0] for r in c.execute("SELECT v FROM t ORDER BY v")))' "$1"; }
if [[ -s "$SRC/state.db-wal" ]] && case_ "state export" 0 python3 infra/railway/hermes/hermes_state.py export "$SRC" "$TMP/hermes-data.tgz"; then
  tar -tzf "$TMP/hermes-data.tgz" | sort >"$TMP/state.list"
  has '^state\.db$' "$TMP/state.list" && has '^profiles/coder/state\.db$' "$TMP/state.list" && has '^sessions/s1\.json$' "$TMP/state.list" &&
    has '^plugins/aios$' "$TMP/state.list" && ! has '-wal$|-shm$|aios-bak|aios-setup-ok|pre-import' "$TMP/state.list" &&
    [[ "$(stat -c %a "$TMP/hermes-data.tgz")" == 600 ]] &&
    ok "state export: SQLite copies without -wal/-shm, no transfer artifacts or markers, archive 0600" || bad "state export content"
fi
crash_db "$DST/state.db" cloud 50
mkdir -p "$DST/sessions" && echo '{"id": "cloud"}' >"$DST/sessions/cloud.json"
cp "$TMP/hermes-data.tgz" "$DST/hermes-import.tgz"
: >"$H/calls"
if [[ -s "$DST/state.db-wal" ]] && case_ "hermes import" 0 env "${HENV[@]}" HERMES_HOME="$DST" sh infra/railway/hermes/start.sh; then
  aside=$(find "$DST" -maxdepth 1 -name '.pre-import-*' -print -quit)
  [[ ! -e "$DST/state.db-wal" && ! -e "$DST/state.db-shm" && ! -e "$DST/hermes-import.tgz" && ! -e "$DST/sessions/cloud.json" ]] &&
    [[ "$(db_rows "$DST/state.db")" == "ok local-0,local-1,local-2,local-3,local-4" ]] &&
    [[ "$(db_rows "$DST/profiles/coder/state.db")" == "ok coder-0,coder-1" ]] &&
    [[ "$(readlink "$DST/plugins/aios")" == /opt/aios/plugins/aios ]] &&
    [[ -n "$aside" && -s "$aside/state.db-wal" && -e "$aside/sessions/cloud.json" ]] &&
    has '^hermes gateway run' "$H/calls" && has '^[0-9]{4}-' "$DST/.aios-setup-ok" &&
    ok "hermes: pending import replaces the volume before setup/gateway; foreign WAL moved aside, local rows intact" ||
    bad "hermes import"
fi
F=$H/broken
mkdir -p "$F" && echo keep >"$F/state.db" && echo ok >"$F/.aios-setup-ok"
head -c 200 "$TMP/hermes-data.tgz" >"$F/hermes-import.tgz"
: >"$H/calls"
if run 1 env "${HENV[@]}" HERMES_HOME="$F" sh infra/railway/hermes/start.sh; then
  has 'state import failed' "$OUT" && ! has '^hermes' "$H/calls" && [[ -e "$F/hermes-import.tgz" && "$(cat "$F/state.db")" == keep ]] &&
    [[ -z "$(find "$F" -maxdepth 1 -name '.pre-import-*' -o -maxdepth 1 -name '.import-staging-*')" ]] &&
    ok "hermes: a corrupt import archive stops the boot and leaves the volume untouched" || bad "hermes broken import"
else bad "hermes started after a failed import"; fi
python3 - "$TMP/evil.tgz" <<'PY'
import io, sys, tarfile
with tarfile.open(sys.argv[1], "w:gz") as tar:
    data = b"x"
    info = tarfile.TarInfo("../escaped")
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))
PY
if run 1 python3 infra/railway/hermes/hermes_state.py import "$TMP/evil.tgz" "$TMP/evil-dest"; then
  has 'unsafe archive member' "$OUT" && [[ ! -e "$TMP/escaped" ]] && ok "state import refuses members outside the directory" ||
    bad "state import traversal"
else bad "state import accepted a traversal member"; fi
mkdir -p "$TMP/locked-dest/root-owned" && echo keep >"$TMP/locked-dest/state.db" && chmod 555 "$TMP/locked-dest/root-owned"
if run 1 python3 infra/railway/hermes/hermes_state.py import "$TMP/hermes-data.tgz" "$TMP/locked-dest"; then
  has 'cannot move .*root-owned' "$OUT" && [[ "$(find "$TMP/locked-dest" -mindepth 1 -maxdepth 1 -printf '%f\n' | sort | tr '\n' ' ')" == "root-owned state.db " ]] &&
    ok "state import checks every entry can move aside before changing anything" || bad "state import preflight"
else bad "state import ignored an entry it cannot move"; fi
chmod 755 "$TMP/locked-dest/root-owned"

if case_ "edge-gw dry" 0 env EDGE_GW_DRY_RUN=1 EDGE_GW_FORWARD='8093=thinkpad:8093,11434=thinkpad:11434' \
  EDGE_GW_EXPOSE='8092=knowledge.railway.internal:8080' sh infra/railway/edge-gw/entrypoint.sh; then
  has 'TCP6-LISTEN:8093,ipv6only=0.*tailscale --socket=[^ ]+ nc thinkpad 8093' "$OUT" &&
    has 'serve --bg --yes --tcp 8092 tcp://127.0.0.1:8092' "$OUT" && has 'advertise-tags=tag:aios-cloud' "$OUT" &&
    ! has 'funnel' "$OUT" && ok "edge-gw: userspace tailscale, private-net forwards, tailnet-only serve" || bad "edge-gw plan"
fi
if run 1 env EDGE_GW_DRY_RUN=1 EDGE_GW_FORWARD='8093=thinkpad:8093;id' sh infra/railway/edge-gw/entrypoint.sh; then
  ok "edge-gw rejects malformed forward specs"
else bad "edge-gw accepted a malformed spec"; fi
if run 1 env EDGE_GW_FORWARD='8093=thinkpad:8093' sh infra/railway/edge-gw/entrypoint.sh; then
  has 'TS_AUTHKEY is required' "$OUT" && ! [[ -s "$SHIM_LOG" ]] && ok "edge-gw refuses to start without TS_AUTHKEY" ||
    bad "edge-gw authkey"
else bad "edge-gw started without TS_AUTHKEY"; fi

BK=infra/railway/db-backup/backup.sh
grep -qx 'RCLONE_CONFIG_AIOS_FORCE_PATH_STYLE=false' infra/railway/db-backup/variables.env &&
  ok "db-backup: rclone uses virtual-hosted-style requests (Railway Buckets)" || { : >"$OUT"; bad "db-backup URL style"; }
if case_ "db-backup" 0 env PGHOST=postgres.railway.internal PGPASSWORD=pg-secret BACKUP_REMOTE=aios:bkt/postgres bash $BK; then
  has '^pg_dump	-Fc --no-password -d aios' "$SHIM_LOG" && has '^rclone	rcat --s3-no-check-bucket aios:bkt/postgres/[0-9TZ]+/litellm.dump' "$SHIM_LOG" &&
    has '^rclone	delete --min-age 14d' "$SHIM_LOG" && ! has 'pg-secret' "$SHIM_LOG" &&
    ok "db-backup: streams each database to the bucket and prunes" || bad "db-backup"
fi
if run 1 env PGHOST=x PGPASSWORD=y BACKUP_REMOTE=aios:bkt SHIM_RCLONE_FAIL=rcat bash $BK; then
  has '^rclone	deletefile' "$SHIM_LOG" && ok "db-backup: failed upload fails the cron run and removes the partial" ||
    bad "db-backup failure cleanup"
else bad "db-backup hid a failure"; fi

if command -v ssh-keygen >/dev/null; then
  SB=$TMP/sandbox
  mkdir -p "$SB"
  ssh-keygen -q -t ed25519 -N '' -f "$SB/hostkey" >/dev/null
  cmd=$(python3 -c 'import json,shlex,sys; print(shlex.split(json.load(open(sys.argv[1]))["deploy"]["startCommand"])[2])' \
    infra/railway/sandbox/railway.json)
  cmd=${cmd//\/etc\/ssh\/keys/$SB/keys}
  cmd=${cmd//\/run\/aios-authorized_keys/$SB/authorized_keys}
  cmd=${cmd//\/etc\/profile.d\/aios-railway.sh/$SB/profile.sh}
  cmd=${cmd//exec \/usr\/bin\/tini -- \/usr\/local\/sbin\/aios-sandbox-entrypoint/exec env}
  host_b64=$(base64 -w0 "$SB/hostkey")
  if case_ "sandbox start" 0 env AIOS_SANDBOX_HOST_KEY_B64="$host_b64" AIOS_SANDBOX_SSH_PUBKEY="ssh-ed25519 AAAATEST hermes" \
    AIOS_EDGE_URL=http://edge-gw.railway.internal:8093 bash -c "$cmd"; then
    cmp -s "$SB/keys/ssh_host_ed25519_key" "$SB/hostkey" && has 'ssh-ed25519' "$SB/keys/ssh_host_ed25519_key.pub" &&
      has '^ssh-ed25519 AAAATEST hermes$' "$SB/authorized_keys" && has "^AIOS_AUTHORIZED_KEYS=$SB/authorized_keys" "$OUT" &&
      ! has 'AIOS_SANDBOX_HOST_KEY_B64' "$OUT" &&
      ok "sandbox startCommand: stable host key + authorized key from variables, key var not inherited" ||
      bad "sandbox startCommand"
    has '^export AIOS_EDGE_URL=http://edge-gw.railway.internal:8093$' "$SB/profile.sh" && [[ "$(stat -c %a "$SB/profile.sh")" == 644 ]] &&
      ok "sandbox startCommand: AIOS_EDGE_URL exported to the agent's login profile (Hermes snapshots bash -l)" ||
      bad "sandbox AIOS_EDGE_URL profile"
  fi
  if run 1 env AIOS_SANDBOX_HOST_KEY_B64= AIOS_SANDBOX_SSH_PUBKEY="ssh-ed25519 AAAATEST hermes" bash -c "$cmd"; then
    has 'AIOS_SANDBOX_HOST_KEY_B64: is required on Railway' "$OUT" &&
      ok "sandbox startCommand fails fast without a stable host key" || bad "sandbox host key message"
  else bad "sandbox started without a stable host key"; fi
  if run 1 env AIOS_SANDBOX_HOST_KEY_B64="$host_b64" AIOS_SANDBOX_SSH_PUBKEY= bash -c "$cmd"; then
    has 'AIOS_SANDBOX_SSH_PUBKEY: is required on Railway' "$OUT" &&
      ok "sandbox startCommand fails fast without the Hermes public key" || bad "sandbox pubkey message"
  else bad "sandbox started without an authorized key"; fi
  if run 1 env AIOS_SANDBOX_HOST_KEY_B64="$host_b64" AIOS_SANDBOX_SSH_PUBKEY="ssh-ed25519 AAAATEST hermes" \
    AIOS_EDGE_URL='http://edge-gw:8093;id' bash -c "$cmd"; then
    has 'AIOS_EDGE_URL must be' "$OUT" && ok "sandbox startCommand rejects a malformed AIOS_EDGE_URL" || bad "edge url message"
  else bad "sandbox accepted a malformed AIOS_EDGE_URL"; fi
fi
if grep -qxF 'AIOS_EDGE_URL=http://${{edge-gw.RAILWAY_PRIVATE_DOMAIN}}:8093' infra/railway/sandbox/variables.env; then
  ok "sandbox: AIOS_EDGE_URL points the media skill at edge-gw:8093"
else : >"$OUT"; bad "sandbox AIOS_EDGE_URL"
fi

# ---------------------------------------------------------------------------------------------------------
section "mk/railway.mk"
targets=$(grep -oE '^[a-z-]+:' mk/railway.mk | tr -d ':' | sort)
for t in railway-plan db-migrate-dry storage-sync-dry tailscale-edge-dry restore-drill; do
  grep -qx "$t" <<<"$targets" && grep -qE "^$t: ## " mk/railway.mk && ok "target $t (with help)" || { : >"$OUT"; bad "target $t"; }
done
grep -q -- '--apply' mk/railway.mk && { : >"$OUT"; bad "a make target passes --apply"; } || ok "no make target can apply"
if make -n -f mk/railway.mk railway-plan db-migrate-dry storage-sync-dry tailscale-edge-dry restore-drill >"$OUT" 2>&1; then
  ok "make -n parses mk/railway.mk"
else bad "make -n mk/railway.mk"; fi

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
((FAIL == 0))
