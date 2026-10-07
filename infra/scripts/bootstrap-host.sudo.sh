#!/usr/bin/env bash
# One-time, idempotent host bootstrap for the AI Agent OS V1 (Linux Mint 22 "Wilma" = Ubuntu 24.04 noble).
#   sudo bash infra/scripts/bootstrap-host.sudo.sh
# Then LOG OUT and back in (docker/render group membership).
# Installs: Docker Engine + Compose plugin, ffmpeg, jq, gh, postgresql-client, sqlite3, vulkan-tools,
#           Ollama (system service, Vulkan on the Radeon 780M). Go and uv are user-space (already in ~/.local).
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run with: sudo bash $0"; exit 1; }
TARGET_USER="${SUDO_USER:?run via sudo from your normal user}"
COMPOSE_SUBNET="172.30.0.0/24" # must match networks.aios in compose.yaml
INSTALL_OLLAMA="${INSTALL_OLLAMA:-1}"
INSTALL_TAILSCALE="${INSTALL_TAILSCALE:-0}"
log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

CODENAME="$(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")"
[[ "$CODENAME" == "noble" ]] || { echo "Expected Ubuntu base 'noble', got '$CODENAME'"; exit 1; }
ARCH="$(dpkg --print-architecture)"

# ufw must default-deny incoming, because Ollama binds 0.0.0.0 so containers can reach it.
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw status verbose | grep -q "deny (incoming)" || {
    echo "ufw is active but the default incoming policy is not 'deny'. Fix with: ufw default deny incoming"; exit 1; }
  UFW_ACTIVE=1
else
  echo "WARNING: ufw is not active - Ollama on 0.0.0.0:11434 would be reachable from the LAN."
  echo "         Enable it first (sudo ufw enable) or rerun with INSTALL_OLLAMA=0."
  [[ "$INSTALL_OLLAMA" == 0 ]] || exit 1
  UFW_ACTIVE=0
fi

log "Base packages"
apt-get update
apt-get install -y ca-certificates curl wget gnupg zstd

log "Docker apt repo (deb822, Suites=$CODENAME - Mint's own codename would break it)"
install -m 0755 -d /etc/apt/keyrings
[[ -s /etc/apt/keyrings/docker.asc ]] || curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
cat >/etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: ${CODENAME}
Components: stable
Architectures: ${ARCH}
Signed-By: /etc/apt/keyrings/docker.asc
EOF

log "GitHub CLI apt repo"
if [[ ! -s /etc/apt/keyrings/githubcli-archive-keyring.gpg ]]; then
  tmp="$(mktemp)"
  wget -nv -O "$tmp" https://cli.github.com/packages/githubcli-archive-keyring.gpg
  install -m 0644 "$tmp" /etc/apt/keyrings/githubcli-archive-keyring.gpg
  rm -f "$tmp"
fi
echo "deb [arch=${ARCH} signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
  >/etc/apt/sources.list.d/github-cli.list

log "Packages"
apt-get update
apt-get install -y \
  docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin \
  ffmpeg jq gh postgresql-client sqlite3 vulkan-tools mesa-vulkan-drivers

log "Docker daemon.json (published ports default to loopback; log rotation)"
install -d -m 0755 /etc/docker
DOCKER_RESTART=0
if [[ ! -f /etc/docker/daemon.json ]]; then
  cat >/etc/docker/daemon.json <<'EOF'
{
  "ip": "127.0.0.1",
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" }
}
EOF
  DOCKER_RESTART=1
else
  echo "daemon.json exists - left untouched (wanted: \"ip\": \"127.0.0.1\" + json-file log rotation)"
fi
systemctl enable --now containerd.service docker.service
[[ "$DOCKER_RESTART" == 1 ]] && systemctl restart docker

log "Groups for ${TARGET_USER} (docker group = root-equivalent; render/video = GPU)"
groupadd -f docker
usermod -aG docker,render,video "$TARGET_USER"

if [[ "$INSTALL_OLLAMA" == 1 ]]; then
  log "Ollama (official installer; Vulkan backend is used on gfx1103 - ROCm does not support the 780M)"
  command -v ollama >/dev/null || curl -fsSL https://ollama.com/install.sh | sh
  install -d -m 0755 /etc/systemd/system/ollama.service.d
  cat >/etc/systemd/system/ollama.service.d/override.conf <<'EOF'
[Service]
Environment="OLLAMA_HOST=0.0.0.0:11434"
Environment="OLLAMA_CONTEXT_LENGTH=4096"
Environment="OLLAMA_FLASH_ATTENTION=1"
Environment="OLLAMA_KV_CACHE_TYPE=q8_0"
Environment="OLLAMA_KEEP_ALIVE=5m"
Environment="OLLAMA_MAX_LOADED_MODELS=1"
Environment="OLLAMA_NUM_PARALLEL=1"
# Vulkan is on by default. To force CPU: Environment="OLLAMA_VULKAN=0"
EOF
  OLLAMA_BIN="$(readlink -f "$(command -v ollama)")"
  setcap cap_perfmon+ep "$OLLAMA_BIN" && echo "setcap ok on $OLLAMA_BIN (re-run this script after Ollama upgrades)" ||
    echo "WARN: setcap failed on $OLLAMA_BIN - Ollama will estimate VRAM from model size"
  systemctl daemon-reload
  systemctl enable ollama
  systemctl restart ollama
  if [[ "$UFW_ACTIVE" == 1 ]]; then
    log "ufw: only the compose subnet may reach host Ollama"
    ufw allow from "$COMPOSE_SUBNET" to any port 11434 proto tcp comment 'aios compose -> ollama'
  fi
fi

if [[ "$INSTALL_TAILSCALE" == 1 ]]; then
  log "Tailscale (installer maps linuxmint -> ubuntu/noble)"
  command -v tailscale >/dev/null || curl -fsSL https://tailscale.com/install.sh | sh
  echo "Run 'sudo tailscale up' yourself to log in."
fi

log "Done. Now LOG OUT and back in, then run:"
cat <<'EOF'
  cd ~/agent-system && make doctor
EOF
