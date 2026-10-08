#!/usr/bin/env bash
# Expose the ThinkPad edge worker (:8093) and Ollama (:11434) to the tailnet ONLY (ARCHITECTURE §22.5, §24.17),
# so the Railway control plane (edge-gw, tag:aios-cloud) can use them. Uses `tailscale serve --tcp` (tailnet-only
# TCP forwarding to 127.0.0.1); never `tailscale funnel`, which would publish them on the internet.
# DRY-RUN by default; --apply configures serve, --off removes it. Always prints the ACL policy to install.
#
# Install Tailscale: INSTALL_TAILSCALE=1 sudo bash infra/scripts/bootstrap-host.sudo.sh  (or the official
# `curl -fsSL https://tailscale.com/install.sh | sh`), then `sudo tailscale up --advertise-tags=tag:thinkpad`
# and, once, `sudo tailscale set --operator=$USER` so serve works without sudo.
set -euo pipefail

usage() {
  cat <<'EOF'
usage: tailscale-edge.sh [--apply] [--off] [--acl] [--ports 8093,11434]
  (default)  show what would be served and the ACL policy; changes nothing
  --apply    tailscale serve --bg --tcp <port> tcp://127.0.0.1:<port> for each port
  --off      with --apply: remove those serve entries
  --acl      only print the ACL policy
EOF
}

APPLY=0 OFF=0 ACL_ONLY=0
PORTS="${EDGE_TAILNET_PORTS:-8093,11434}"
while (($#)); do
  case "$1" in
    --apply) APPLY=1 ;;
    --off) OFF=1 ;;
    --acl) ACL_ONLY=1 ;;
    --ports) PORTS="${2:?--ports needs a list}"; shift ;;
    -h | --help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
IFS=',' read -r -a PORT_LIST <<<"${PORTS// /}"
for p in "${PORT_LIST[@]}"; do [[ "$p" =~ ^[0-9]{1,5}$ && "$p" -ge 1 && "$p" -le 65535 ]] || die "invalid port: $p"; done

acl() {
  local ip
  ip=$(printf '"tcp:%s", ' "${PORT_LIST[@]}" | sed 's/, $//')
  cat <<EOF
// Tailscale policy file (admin console -> Access controls). Merge into your policy, and make sure no
// allow-all rule remains, otherwise every device reaches every port anyway.
{
  "tagOwners": {
    "tag:thinkpad":   ["autogroup:admin"],
    "tag:aios-cloud": ["autogroup:admin"],
  },
  "grants": [
    // Railway edge-gw -> ThinkPad: edge worker and Ollama, nothing else
    {"src": ["tag:aios-cloud"], "dst": ["tag:thinkpad"], "ip": [$ip]},
    // ThinkPad (edge worker, kb CLI, curl) -> cloud APIs relayed by edge-gw (EDGE_GW_EXPOSE); each needs its bearer key
    {"src": ["tag:thinkpad"], "dst": ["tag:aios-cloud"], "ip": ["tcp:4000", "tcp:8090", "tcp:8092", "tcp:8642"]},
    // your own devices (phone, other laptop) -> Hermes API and approvals; widen only if needed
    {"src": ["autogroup:member"], "dst": ["tag:aios-cloud"], "ip": ["tcp:8090", "tcp:8642"]},
  ],
  // legacy ACL equivalent of the first grant:
  //   "acls": [{"action": "accept", "src": ["tag:aios-cloud"], "dst": ["tag:thinkpad:$(IFS=,; echo "${PORT_LIST[*]}")"]}]
  "tests": [
    {"src": "tag:aios-cloud", "accept": ["tag:thinkpad:${PORT_LIST[0]}"], "deny": ["tag:thinkpad:22", "tag:thinkpad:5432"]},
  ],
}
EOF
}

if ((ACL_ONLY)); then acl; exit 0; fi

command -v tailscale >/dev/null || die "tailscale is not installed (see the header of this script)"
command -v python3 >/dev/null || die "python3 is required"
status_json=$(tailscale status --json 2>/dev/null) || die "tailscaled is not running (sudo systemctl enable --now tailscaled)"
read -r state dns tags <<<"$(python3 -c '
import json, sys
s = json.load(sys.stdin)
me = s.get("Self") or {}
print(s.get("BackendState", "?"), (me.get("DNSName") or "?").rstrip("."), ",".join(me.get("Tags") or []) or "-")
' <<<"$status_json")"
[[ "$state" == Running ]] || die "tailscale is $state: log in with sudo tailscale up --advertise-tags=tag:thinkpad"
echo "tailnet: logged in as ${dns} (tags: ${tags})"
[[ ",$tags," == *",tag:thinkpad,"* ]] ||
  echo "WARNING: this machine is not tag:thinkpad; the ACL below will not match it (sudo tailscale up --advertise-tags=tag:thinkpad)"

serve_json=$(tailscale serve status --json 2>/dev/null || echo '{}')
read -r funnel current <<<"$(python3 -c '
import json, sys
raw = sys.stdin.read().strip() or "{}"
cfg = json.loads(raw) if raw.startswith("{") else {}
funnel = [k for k, v in (cfg.get("AllowFunnel") or {}).items() if v]
tcp = [p + "->" + (h.get("TCPForward") or "web") for p, h in (cfg.get("TCP") or {}).items()]
print(",".join(funnel) or "-", ",".join(tcp) or "-")
' <<<"$serve_json")"
[[ "$funnel" == - ]] || die "Funnel is ON for $funnel: the internet can reach this machine. Turn it off (tailscale funnel reset) and re-run."
echo "current serve: $current"

for p in "${PORT_LIST[@]}"; do
  case "$p" in
    8093) probe="http://127.0.0.1:8093/healthz" ;;
    11434) probe="http://127.0.0.1:11434/api/version" ;;
    *) probe="" ;;
  esac
  if [[ -n "$probe" ]] && ! curl -fsS -m 3 "$probe" >/dev/null 2>&1; then
    echo "WARNING: nothing answers on $probe yet (serve still works once it starts)"
  fi
done

cmds=()
for p in "${PORT_LIST[@]}"; do
  if ((OFF)); then cmds+=("tailscale serve --tcp=$p tcp://127.0.0.1:$p off"); else cmds+=("tailscale serve --bg --yes --tcp $p tcp://127.0.0.1:$p"); fi
done

echo
if ((!APPLY)); then
  echo "DRY-RUN: would run"
  printf '  %s\n' "${cmds[@]}"
else
  for c in "${cmds[@]}"; do
    echo "+ $c"
    # shellcheck disable=SC2086  # fixed, validated words
    $c || die "'$c' failed (permission denied? run once: sudo tailscale set --operator=\$USER)"
  done
  tailscale serve status || true
fi

host="${dns%%.*}"
cat <<EOF

Railway edge-gw variables for this machine:
  EDGE_GW_FORWARD=$(for p in "${PORT_LIST[@]}"; do printf '%s=%s:%s,' "$p" "$host" "$p"; done | sed 's/,$//')
Ollama listens on 0.0.0.0 for the compose subnet; ufw keeps the LAN out. Do not add 'ufw allow in on tailscale0':
serve handles tailnet traffic inside tailscaled.

EOF
acl
