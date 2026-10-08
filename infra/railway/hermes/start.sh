#!/bin/sh
# Railway start for the Hermes gateway. The image's main-wrapper.sh has already run the stage2 bootstrap
# (volume chown, stock config seeding) and dropped to the `hermes` user before exec'ing this script, on both
# the s6 (PID 1) and the direct-bootstrap paths. Steps:
#   1. AIOS_MAINTENANCE=1: answer the health check only; the gateway never starts (RUNBOOK §9);
#   2. install the sandbox SSH key from a variable (Railway has no file mounts);
#   3. import a pending state archive ($HERMES_HOME/hermes-import.tgz, uploaded at the cutover) before anything
#      opens state.db; the previous content moves aside, so no foreign -wal/-shm survives next to it;
#   4. re-apply the AIOS Hermes setup (idempotent: config, profiles, plugin link, cron);
#   5. run the gateway.
set -eu

log() { printf '{"level":"%s","component":"hermes-start","msg":"%s"}\n' "$1" "$2" >&2; }

lib="${AIOS_RAILWAY_LIB:-/opt/aios/railway}"
py="${AIOS_PYTHON:-/opt/hermes/.venv/bin/python}"
home="${HERMES_HOME:-/opt/data}"
# shellcheck source-path=SCRIPTDIR source=../lib/maintenance.sh
. "$lib/maintenance.sh"
aios_maintenance_hold "$py" /health "${PORT:-8642}" hermes-start

key_file="${TERMINAL_SSH_KEY:-/opt/aios/ssh/id_ed25519}"
if [ -n "${AIOS_SANDBOX_SSH_KEY_B64:-}" ]; then
  umask 077
  printf '%s' "$AIOS_SANDBOX_SSH_KEY_B64" | base64 -d >"$key_file.tmp"
  mv "$key_file.tmp" "$key_file"
  log info "sandbox SSH key installed at $key_file"
elif [ ! -s "$key_file" ]; then
  log warn "AIOS_SANDBOX_SSH_KEY_B64 is empty: terminal tools and pre_verify checks will fail"
fi
unset AIOS_SANDBOX_SSH_KEY_B64

pending="$home/hermes-import.tgz"
if [ -f "$pending" ]; then
  log info "importing $pending; the previous content moves to $home/.pre-import-*"
  if ! "$py" "$lib/hermes_state.py" import "$pending" "$home"; then
    log error "state import failed; $pending was kept and the gateway was not started"
    exit 1
  fi
  rm -f "$pending"
fi

# The stage2 hook seeds a stock config.yaml before this script runs, so "a config exists" proves nothing: only
# the marker written after a successful setup.py run means the AIOS config (plugin guards, sandbox terminal,
# LiteLLM routing) is in place. A failed re-apply keeps the gateway up on that config; without the marker the
# gateway would run on Hermes' stock config, so that is fatal.
marker="$home/.aios-setup-ok"
# AIOS_SETUP_ARGS is a list of setup.py flags (e.g. "--recreate-cron"), split on purpose.
# shellcheck disable=SC2086
if "$py" "${AIOS_SETUP:-/opt/aios/bin/setup.py}" ${AIOS_SETUP_ARGS:-}; then
  date -u +%Y-%m-%dT%H:%M:%SZ >"$marker" || log warn "could not write $marker"
  log info "aios setup applied; starting hermes gateway"
elif [ -f "$marker" ]; then
  log error "aios setup failed; starting the gateway on the last applied AIOS config (see the lines above)"
else
  log error "aios setup failed and was never applied on this volume; refusing to start a gateway on the stock config"
  exit 1
fi
exec "${HERMES_BIN:-hermes}" gateway run
