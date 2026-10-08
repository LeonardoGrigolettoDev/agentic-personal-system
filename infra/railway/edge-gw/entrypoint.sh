#!/bin/sh
# Tailscale gateway between Railway's private network and the tailnet (ARCHITECTURE §22.5, §24.17).
#   EDGE_GW_FORWARD "<listen>=<tailnet-host>:<port>,..."      Railway services -> ThinkPad (edge :8093, Ollama :11434)
#   EDGE_GW_EXPOSE  "<tailnet-port>=<railway-host>:<port>,..." tailnet (ACL-gated) -> Railway services (optional)
# tailscaled runs in userspace mode (no /dev/net/tun on Railway) as a tagged node. With TS_STATE on the volume the
# node keeps its name and 100.x address across deploys and TS_AUTHKEY is only needed for the first login;
# TS_STATE=mem: + an ephemeral key gives a fresh node per deploy instead. Funnel is never used.
set -eu

TS_HOSTNAME="${TS_HOSTNAME:-aios-railway}"
TS_TAGS="${TS_TAGS:-tag:aios-cloud}"
TS_STATE="${TS_STATE:-mem:}"
SOCK="${TS_SOCKET:-/tmp/tailscaled.sock}"
FORWARD="${EDGE_GW_FORWARD:-}"
EXPOSE="${EDGE_GW_EXPOSE:-}"
DRY="${EDGE_GW_DRY_RUN:-0}"

log() { printf '{"level":"%s","component":"edge-gw","msg":"%s"}\n' "$1" "$2" >&2; }
die() { log error "$1"; exit 1; }
run() { if [ "$DRY" = 1 ]; then echo "+ $*"; else "$@"; fi; }

# "a=b:c,d=e:f" -> one "a b c" line per item; rejects anything else so the values never reach a shell unquoted
parse() {
  echo "$1" | tr ',' '\n' | while IFS= read -r item; do
    item=$(echo "$item" | tr -d ' ')
    [ -z "$item" ] && continue
    echo "$item" | grep -Eq '^[0-9]{1,5}=[A-Za-z0-9][A-Za-z0-9.-]*:[0-9]{1,5}$' || { echo "BAD $item"; continue; }
    echo "$item" | sed -E 's/^([0-9]+)=([^:]+):([0-9]+)$/\1 \2 \3/'
  done
}

fwd=$(parse "$FORWARD")
exp=$(parse "$EXPOSE")
bad=$(printf '%s\n%s\n' "$fwd" "$exp" | grep '^BAD' || true)
[ -z "$bad" ] || die "invalid EDGE_GW_FORWARD/EDGE_GW_EXPOSE item(s): $(echo "$bad" | cut -c5- | tr '\n' ' ')"
[ -n "$fwd$exp" ] || die "nothing to do: set EDGE_GW_FORWARD (and optionally EDGE_GW_EXPOSE)"
dups=$(printf '%s\n%s\n' "$fwd" "$exp" | awk 'NF{print $1}' | sort | uniq -d)
[ -z "$dups" ] || die "port(s) used twice: $(echo "$dups" | tr '\n' ' ')"
logged_in=0
[ "$TS_STATE" != "mem:" ] && [ -s "$TS_STATE" ] && logged_in=1
[ "$DRY" = 1 ] || [ -n "${TS_AUTHKEY:-}" ] || [ "$logged_in" = 1 ] ||
  die "TS_AUTHKEY is required for the first login (tagged auth key or OAuth client secret)"
[ "$TS_STATE" = "mem:" ] || [ "$DRY" = 1 ] || mkdir -p "$(dirname "$TS_STATE")"

pids=""
cleanup() { for p in $pids; do kill "$p" 2>/dev/null || true; done; }
trap 'cleanup; exit 0' INT TERM

run tailscaled --tun=userspace-networking --state="$TS_STATE" --socket="$SOCK" &
pids="$pids $!"
if [ "$DRY" != 1 ]; then
  i=0
  until tailscale --socket="$SOCK" version >/dev/null 2>&1 && [ -S "$SOCK" ]; do
    i=$((i + 1)); [ "$i" -le 30 ] || die "tailscaled did not start"
    sleep 1
  done
  auth=""
  if [ -n "${TS_AUTHKEY:-}" ]; then
    keyfile=$(mktemp); chmod 600 "$keyfile"
    printf '%s' "$TS_AUTHKEY" >"$keyfile"
    auth="--auth-key=file:$keyfile"
  fi
  unset TS_AUTHKEY
  # $auth is empty or one flag without spaces
  # shellcheck disable=SC2086
  tailscale --socket="$SOCK" up $auth --hostname="$TS_HOSTNAME" --advertise-tags="$TS_TAGS" \
    --accept-dns=false --timeout=90s || { rm -f "${keyfile:-}"; die "tailscale up failed"; }
  rm -f "${keyfile:-}"
  log info "joined the tailnet as $TS_HOSTNAME ($TS_TAGS)"
else
  run tailscale --socket="$SOCK" up --auth-key="file:<TS_AUTHKEY>" --hostname="$TS_HOSTNAME" \
    --advertise-tags="$TS_TAGS" --accept-dns=false --timeout=90s
fi

# Railway -> tailnet: listen on the private network (dual stack), dial through tailscaled per connection
for line in $(echo "$fwd" | tr ' ' '|'); do
  listen=${line%%|*}; rest=${line#*|}; host=${rest%%|*}; port=${rest#*|}
  log info "forward [::]:$listen -> tailnet $host:$port"
  run socat "TCP6-LISTEN:$listen,ipv6only=0,reuseaddr,fork" "EXEC:tailscale --socket=$SOCK nc $host $port" &
  pids="$pids $!"
done

# tailnet -> Railway: loopback relay + `tailscale serve --tcp` (serve only proxies to 127.0.0.1)
for line in $(echo "$exp" | tr ' ' '|'); do
  tport=${line%%|*}; rest=${line#*|}; host=${rest%%|*}; port=${rest#*|}
  log info "expose tailnet :$tport -> $host:$port"
  run socat "TCP4-LISTEN:$tport,bind=127.0.0.1,reuseaddr,fork" "TCP:$host:$port" &
  pids="$pids $!"
  run tailscale --socket="$SOCK" serve --bg --yes --tcp "$tport" "tcp://127.0.0.1:$tport"
done

[ "$DRY" = 1 ] && { wait; exit 0; }

# any relay or tailscaled exiting fails the container so Railway restarts it
while :; do
  for p in $pids; do
    kill -0 "$p" 2>/dev/null || { log error "process $p exited"; cleanup; exit 1; }
  done
  sleep 5
done
