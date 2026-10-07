# docker
RAM: These are estimates; nothing has been measured on this machine. dockerd plus containerd idle: about 80-120 MB. Postgres with the pgvector settings above: about 450-700 MB in normal use, capped at 1536 MB. Valkey: about 10-40 MB idle, capped at 320 MB. Base infrastructure (engine, Postgres, Valkey) comes to about 0.6-0.9 GB in normal use and at most about 2 GB. Not counted here: LiteLLM (about 300-600 MB), Langfuse web and worker (about 0.5-1 GB) plus ClickHouse (1-2+ GB) and MinIO if Langfuse v3/v4 needs them, Hermes, and host Ollama with Qwen3 4B (about 3-4 GB while loaded). Docker Desktop would add a VM with a fixed RAM reservation of several GB on top, which is one more reason to use Engine.

Machine facts I checked read-only on this laptop: /etc/os-release has VERSION_CODENAME=wilma and UBUNTU_CODENAME=noble. ufw 0.36.2 is ACTIVE. Docker is not installed. /etc/apt/sources.list.d holds only claude-desktop.list, google-chrome.sources and official-package-repositories.list, so there is no existing docker or docker.io repo to clean up. Every helper package is available from noble: glslc 2023.8, spirv-headers, libvulkan-dev 1.3.275, vulkan-tools, ffmpeg 6.1.1, jq 1.7.1, postgresql-client 16, cmake 3.28, libav*-dev 6.1.1, and golang-go 1.22, which is old. mesa-vulkan-drivers 25.2.8 is already installed, so RADV for the 780M should be there. build-essential and pkg-config are already installed.

DOCKER ENGINE INSTALL. Docker's official Ubuntu page now writes a deb822 file, /etc/apt/sources.list.d/docker.sources, and fills Suites with $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}"). On Mint that comes out as noble, so the official command already handles the Mint gotcha. The breakage comes from older snippets that use lsb_release -cs or VERSION_CODENAME, which gives "wilma". Docker says Mint is "not officially supported (though it may work)". Supported Ubuntu releases are 26.04, 24.04 and 22.04. The latest engine in the release notes is 29.8.2 (2026-09-30). nftables is still an opt-in experimental backend, and iptables is the default. Packages: docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin. Post-install steps: groupadd docker (it may already exist), usermod -aG docker $USER, newgrp docker (or log out and back in), and systemctl enable docker/containerd. Docker warns that the docker group "grants root-level privileges to the user".

UFW. Docker's docs say published-port traffic "gets diverted before it goes through the ufw firewall settings" because it is DNAT'd in the nat table before ufw's INPUT/OUTPUT chains see it. "Publishing container ports is insecure by default." Fix: publish every port as "127.0.0.1:HOST:CONTAINER" (or long syntax host_ip: 127.0.0.1). As a safety net, set daemon.json default-network-opts.bridge."com.docker.network.bridge.host_binding_ipv4"="127.0.0.1". It covers new user-defined networks, which includes Compose networks. The "ip" key covers only the default bridge. Leave Postgres and Valkey unpublished entirely. Other services reach them as postgres:5432 and valkey:6379, and you can use docker compose exec for psql. Publish only UIs you want on localhost (Langfuse 3000, LiteLLM 4000). Use Tailscale for remote access, not open LAN ports. Rotate logs in daemon.json (json-file max-size 10m, max-file 3) to protect the SSD.

CONTAINER TO HOST (Ollama, whisper). In Compose, add extra_hosts: ["host.docker.internal:host-gateway"]. host-gateway resolves to the host's bridge IP, usually 172.17.0.1. On Linux this must be added explicitly; Docker Desktop resolves it by itself. Two things follow, inferred from how networking works and not stated in Docker's docs. (1) The host service must listen on more than loopback. Ollama binds to 127.0.0.1:11434 by default, so set OLLAMA_HOST=0.0.0.0:11434 in a systemd override. (2) Container-to-host traffic DOES go through ufw's INPUT chain, and with ufw active and default-deny incoming it will be dropped. Pin the Compose network subnet (for example 172.30.0.0/24) and allow only that: sudo ufw allow from 172.30.0.0/24 to any port 11434 proto tcp. Because ufw protects the host process, binding Ollama to 0.0.0.0 does not expose it to the LAN.

DESKTOP VS ENGINE. Recommend Docker Engine. On Linux, Docker Desktop runs a QEMU/KVM VM with its own desktop-linux context. It reserves a fixed share of RAM, conflicts with Engine on ports, and complicates host-gateway and file sharing. That is the wrong trade on a 16 GB machine that already has about 8 GB in use. Engine also matches what runs on Railway.

POSTGRES + PGVECTOR. pgvector is at 0.8.7. The tags are pg18 / 0.8.7-pg18 (bookworm), pg18-trixie / 0.8.7-pg18-trixie, and the same set for pg17. The official postgres image is at 18.6 and 17.11. Recommend pinning pgvector/pgvector:0.8.7-pg18-trixie. PG18 has been GA for about a year, and Railway's pgvector template also uses PG18. Gotcha: from 18 on, the image's PGDATA is /var/lib/postgresql/18/docker and VOLUME is /var/lib/postgresql, so mount the volume at /var/lib/postgresql, NOT /var/lib/postgresql/data. If you choose pg17, keep /var/lib/postgresql/data. pgvector says HNSW builds are fastest when the graph fits in maintenance_work_mem, and in Docker shm_size must be >= maintenance_work_mem. HNSW limits: vector up to 2000 dims, halfvec up to 4000. Laptop tuning: give the container a 1.5 GB memory limit and set shared_buffers=384MB, effective_cache_size=1GB, work_mem=16MB, maintenance_work_mem=256MB, max_connections=60, max_parallel_maintenance_workers=2, random_page_cost=1.1, wal_compression=on, shm_size=512mb. Raise maintenance_work_mem for one-off bulk HNSW builds with SET in that session. The host's postgresql-client is 16. psql works against an 18 server, but pg_dump 16 refuses to dump an 18 server, so run pg_dump inside the container or add the PGDG repo for postgresql-client-18.

REDIS/VALKEY. Valkey tags: 9.1.2 (latest), 9.0, 8.1 and -alpine variants. Redis 8.x is tri-licensed (RSALv2, SSPLv1 or AGPLv3). Langfuse officially supports Redis >=7 and Valkey >=8, and REQUIRES maxmemory-policy=noeviction. Recommend valkey/valkey:9.1-alpine with BSD-licensed, drop-in Redis protocol: --maxmemory 256mb --maxmemory-policy noeviction --appendonly yes. With noeviction, LiteLLM cache keys must have TTLs. Alternatively, run a second tiny Valkey (allkeys-lru) for the cache.

OTHER TOOLS. gh comes from the cli.github.com apt repo, keyring at /etc/apt/keyrings/githubcli-archive-keyring.gpg. uv uses the user-space curl installer and puts uv and uvx in ~/.local/bin. Tailscale: install.sh handles linuxmint explicitly and maps it to ubuntu/$UBUNTU_CODENAME, so the one-liner works. A manual noble apt route is also available. whisper.cpp Vulkan: libvulkan-dev, glslc and spirv-headers (llama.cpp docs say spirv-headers is not always pulled in), cmake -B build -DGGML_VULKAN=1. Optional: -DWHISPER_COMMON_FFMPEG=yes with libavcodec-dev, libavformat-dev and libavutil-dev. Check with vulkaninfo --summary.

## CMDS
# ===== 1. Docker Engine (official, Mint-safe via UBUNTU_CODENAME) =====
sudo apt update
sudo apt install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
sudo tee /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
grep Suites /etc/apt/sources.list.d/docker.sources   # must print: Suites: noble
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker.service containerd.service
sudo groupadd docker 2>/dev/null; sudo usermod -aG docker $USER
newgrp docker            # or log out/in
docker run --rm hello-world && docker compose version

# ===== 2. /etc/docker/daemon.json (then: sudo systemctl restart docker) =====
{
  "default-network-opts": { "bridge": { "com.docker.network.bridge.host_binding_ipv4": "127.0.0.1" } },
  "ip": "127.0.0.1",
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" }
}

# ===== 3. ufw: let compose network reach host Ollama (11434) / whisper server (e.g. 8178) =====
sudo ufw allow from 172.30.0.0/24 to any port 11434 proto tcp
# Ollama listen beyond loopback:
sudo systemctl edit ollama   # add:
# [Service]
# Environment="OLLAMA_HOST=0.0.0.0:11434"
sudo systemctl daemon-reload && sudo systemctl restart ollama

# ===== 4. Compose fragments =====
networks:
  aios:
    ipam:
      config: [{ subnet: 172.30.0.0/24 }]
services:
  postgres:
    image: pgvector/pgvector:0.8.7-pg18-trixie
    shm_size: 512mb
    mem_limit: 1536m
    environment: { POSTGRES_USER: ${PG_USER}, POSTGRES_PASSWORD: ${PG_PASSWORD}, POSTGRES_DB: ${PG_DB} }
    command: >
      postgres -c shared_buffers=384MB -c effective_cache_size=1GB -c work_mem=16MB
      -c maintenance_work_mem=256MB -c max_connections=60 -c max_parallel_maintenance_workers=2
      -c random_page_cost=1.1 -c wal_compression=on
    volumes: [ "pgdata:/var/lib/postgresql" ]      # PG18+: NOT /var/lib/postgresql/data
    healthcheck: { test: ["CMD-SHELL","pg_isready -U $${POSTGRES_USER}"], interval: 10s, retries: 5 }
    networks: [aios]                                  # no ports: published
  valkey:
    image: valkey/valkey:9.1-alpine
    command: ["valkey-server","--maxmemory","256mb","--maxmemory-policy","noeviction","--appendonly","yes"]
    mem_limit: 320m
    volumes: [ "valkeydata:/data" ]
    networks: [aios]
  litellm:
    extra_hosts: [ "host.docker.internal:host-gateway" ]   # OLLAMA base: http://host.docker.internal:11434
    ports: [ "127.0.0.1:4000:4000" ]
    networks: [aios]
volumes: { pgdata: {}, valkeydata: {} }
# init SQL: CREATE EXTENSION IF NOT EXISTS vector;

# ===== 5. Other apt packages =====
sudo apt install -y ffmpeg jq postgresql-client build-essential cmake pkg-config git \
  libvulkan-dev glslc spirv-headers vulkan-tools mesa-vulkan-drivers \
  libavcodec-dev libavformat-dev libavutil-dev libsdl2-dev
vulkaninfo --summary   # expect RADV / AMD Radeon 780M

# gh (official repo)
(type -p wget >/dev/null || (sudo apt update && sudo apt install wget -y)) \
 && sudo mkdir -p -m 755 /etc/apt/keyrings \
 && out=$(mktemp) && wget -nv -O$out https://cli.github.com/packages/githubcli-archive-keyring.gpg \
 && cat $out | sudo tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null \
 && sudo chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
 && sudo apt update && sudo apt install gh -y

# uv (user space, no sudo) -> ~/.local/bin/uv, uvx
curl -LsSf https://astral.sh/uv/install.sh | sh

# Tailscale (script maps linuxmint -> ubuntu/$UBUNTU_CODENAME)
curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up
# manual alternative:
sudo mkdir -p --mode=0755 /usr/share/keyrings
curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/noble.noarmor.gpg | sudo tee /usr/share/keyrings/tailscale-archive-keyring.gpg >/dev/null
curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/noble.tailscale-keyring.list | sudo tee /etc/apt/sources.list.d/tailscale.list
sudo apt-get update && sudo apt-get install tailscale

# whisper.cpp Vulkan build
git clone https://github.com/ggml-org/whisper.cpp && cd whisper.cpp
cmake -B build -DGGML_VULKAN=1 -DWHISPER_COMMON_FFMPEG=yes
cmake --build build -j --config Release
sh ./models/download-ggml-model.sh base   # or large-v3-turbo / small

# pg_dump against PG18 (host client is 16): use container
docker compose exec postgres pg_dump -U "$PG_USER" "$PG_DB" > backup.sql

## RISKS
- Docker says Linux Mint is 'not officially supported (though it may work)'. Using UBUNTU_CODENAME=noble is the community-standard fix, and the official snippet already resolves to noble on this machine.
- Container-to-host traffic being filtered by ufw INPUT (needs the explicit ufw allow from the compose subnet) is inferred from how Linux networking works; Docker's docs page does not state it. Check with curl http://host.docker.internal:11434/api/tags from inside a container after setup.
- That default-network-opts host_binding_ipv4 applies to Compose-created networks is inferred from the docs ('applies to newly created networks'). Still write 127.0.0.1: explicitly in every ports entry.
- The Postgres memory numbers are my heuristic sizing (25% / 75% rules scaled to a 1.5 GB cap), not official figures. Re-tune after measuring the real workload. Large HNSW builds may need a temporary SET maintenance_work_mem plus a larger shm_size.
- PG18 compatibility of LiteLLM (Prisma) and Langfuse migrations was not explicitly verified. If either fails, fall back to pgvector/pgvector:0.8.7-pg17-trixie with the volume at /var/lib/postgresql/data.
- Langfuse docs mention 'v4'. Its exact infrastructure needs (ClickHouse, S3/MinIO) and RAM were not researched in this task. Sharing one Valkey under noeviction means LiteLLM cache keys must have TTLs.
- The pgvector Hub page shows a pg18 tag dated 'pushed 6 days ago' (0.8.7). That relative date was not cross-checked against a GitHub release date.
- The Docker Engine 29.8.2 (2026-09-30) version and date came from a summarized fetch of the release notes. The CVE IDs in that summary looked unreliable and were left out on purpose.
- The Compose plugin version was not verified.
- Whether the 780M's RADV works with the whisper.cpp Vulkan backend was not tested. Run vulkaninfo first. noble ships glslc 2023.8 and spirv-headers 1.4.309, which should be enough, but the build itself was not tried.
- postgresql-client from noble is v16. Its pg_dump cannot dump an 18 server; use the in-container pg_dump or the PGDG repo.
- golang-go in noble is 1.22, which is old. Install Go from go.dev tarballs if the Decision Service needs a newer Go (not researched here).

## SOURCES
https://docs.docker.com/engine/install/ubuntu/
https://docs.docker.com/engine/install/linux-postinstall/
https://docs.docker.com/engine/network/packet-filtering-firewalls/
https://docs.docker.com/engine/network/port-publishing/
https://docs.docker.com/reference/cli/dockerd/
https://docs.docker.com/reference/cli/docker/container/run/
https://docs.docker.com/reference/compose-file/services/
https://docs.docker.com/engine/release-notes/
https://docs.docker.com/desktop/setup/install/linux/
https://github.com/pgvector/pgvector
https://hub.docker.com/r/pgvector/pgvector/tags
https://hub.docker.com/_/postgres
https://hub.docker.com/_/redis
https://hub.docker.com/r/valkey/valkey
https://langfuse.com/self-hosting/deployment/infrastructure/cache
https://github.com/cli/cli/blob/trunk/docs/install_linux.md
https://docs.astral.sh/uv/getting-started/installation/
https://tailscale.com/kb/1031/install-linux
https://tailscale.com/install.sh
https://pkgs.tailscale.com/stable/#ubuntu-noble
https://github.com/ggml-org/whisper.cpp
https://github.com/ggml-org/llama.cpp/blob/master/docs/build.md
https://oneuptime.com/blog/post/2026-02-08-how-to-install-docker-on-linux-mint/markdown
https://railway.com/deploy/pgvector-just-updated-postgres-18-vector-db-where-hnsw-indexes-actually-build--pgvector-or-just-updated-postgres-18-vec
local: /etc/os-release, apt-cache policy, ufw --version, systemctl is-active ufw (read-only checks)