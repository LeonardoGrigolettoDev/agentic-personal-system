# syntax=docker/dockerfile:1.7
# AIOS sandbox: sshd + toolchains + /workspace. The only place agent shell commands run (Hermes terminal
# backend `ssh`, docs/CONTRACTS.md §6, ARCHITECTURE §21/§24.16). Every download is pinned by sha256;
# amd64 only (bump checksums together with versions: workers/sandbox/tests/verify-downloads.sh --full).
ARG DEBIAN_IMAGE=debian:trixie-slim@sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f
FROM ${DEBIAN_IMAGE}

ARG GO_VERSION=1.27.1
ARG GO_SHA256=63d339f0da5ab53635a56f2490a7984dfe12dfcff22ad749f63edaf590168445
ARG NODE_VERSION=24.21.0
ARG NODE_SHA256=fd8e59d5a511510f6a298afb548f18c7d2b1be404d8b4a27d94fbe49f56cb2d6
ARG UV_VERSION=0.12.23
ARG UV_SHA256=9167d72b3319674b6303c4cbe071854bba13ebdf3d76b1a7cbdc175471fb66d6
ARG DUCKDB_VERSION=1.5.6
ARG DUCKDB_SHA256=cce2687a38681db644c248198b99ecd86170ff47e324c5f33e50c0042f142839
ARG PNPM_VERSION=12.10.1
ARG RUFF_VERSION=0.16.10
ARG ESLINT_VERSION=10.12.0
ARG TYPESCRIPT_VERSION=7.0.2

SHELL ["/bin/bash", "-euo", "pipefail", "-c"]
ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8

# systemd-standalone-sysusers satisfies openssh-server's sysusers dependency (creates the `sshd` privsep
# user) without pulling in systemd. The package's build-time host keys are deleted: keys live in a volume.
RUN [ "$(dpkg --print-architecture)" = amd64 ] || { echo "only amd64 downloads are pinned" >&2; exit 1; } \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      openssh-server systemd-standalone-sysusers tini \
      ca-certificates curl git make jq build-essential python3 python3-venv python3-dev \
      ripgrep fd-find sqlite3 xz-utils unzip procps less tzdata patch file \
 && rm -rf /var/lib/apt/lists/* \
 && rm -f /etc/ssh/ssh_host_* \
 && ln -s /usr/bin/fdfind /usr/local/bin/fd \
 && id sshd

RUN curl -fsSLo /tmp/go.tgz "https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz" \
 && echo "${GO_SHA256}  /tmp/go.tgz" | sha256sum -c - \
 && tar -C /usr/local -xzf /tmp/go.tgz --no-same-owner \
 && rm /tmp/go.tgz \
 && ln -s /usr/local/go/bin/go /usr/local/go/bin/gofmt /usr/local/bin/ \
 && go version

# Node into /usr/local (node/npm/npx/corepack in /usr/local/bin); global eslint/tsc; corepack pnpm shims.
RUN curl -fsSLo /tmp/node.txz "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" \
 && echo "${NODE_SHA256}  /tmp/node.txz" | sha256sum -c - \
 && tar -C /usr/local --strip-components=1 --no-same-owner -xJf /tmp/node.txz \
      "node-v${NODE_VERSION}-linux-x64/bin" "node-v${NODE_VERSION}-linux-x64/include" \
      "node-v${NODE_VERSION}-linux-x64/lib" "node-v${NODE_VERSION}-linux-x64/share" \
 && rm /tmp/node.txz \
 && npm install -g --no-audit --no-fund "eslint@${ESLINT_VERSION}" "typescript@${TYPESCRIPT_VERSION}" \
 && npm cache clean --force \
 && corepack enable pnpm \
 && node --version && eslint --version && tsc --version

RUN curl -fsSLo /tmp/uv.tgz "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-x86_64-unknown-linux-gnu.tar.gz" \
 && echo "${UV_SHA256}  /tmp/uv.tgz" | sha256sum -c - \
 && tar -C /usr/local/bin --strip-components=1 --no-same-owner -xzf /tmp/uv.tgz \
      uv-x86_64-unknown-linux-gnu/uv uv-x86_64-unknown-linux-gnu/uvx \
 && rm /tmp/uv.tgz \
 && UV_TOOL_DIR=/opt/uv/tools UV_TOOL_BIN_DIR=/usr/local/bin UV_PYTHON_DOWNLOADS=never UV_NO_CACHE=1 \
      uv tool install --python /usr/bin/python3 "ruff==${RUFF_VERSION}" \
 && uv --version && ruff --version

RUN curl -fsSLo /tmp/duckdb.gz "https://github.com/duckdb/duckdb/releases/download/v${DUCKDB_VERSION}/duckdb_cli-linux-amd64.gz" \
 && echo "${DUCKDB_SHA256}  /tmp/duckdb.gz" | sha256sum -c - \
 && gunzip -c /tmp/duckdb.gz >/usr/local/bin/duckdb \
 && chmod 0755 /usr/local/bin/duckdb \
 && rm /tmp/duckdb.gz \
 && duckdb -version

# agent: uid/gid 1000 (matches the host owner of data/sandbox/workspace). Password field `*`, not `!`:
# with UsePAM no, sshd refuses accounts whose password starts with `!` (locked), even for key logins.
RUN groupadd --gid 1000 agent \
 && useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash --password '*' agent \
 && install -d -m 0755 -o root -g root /etc/ssh/keys /etc/ssh/authorized_keys /run/sshd /var/lib/wtmpdb \
 && install -d -m 0755 -o agent -g agent /workspace

# pnpm is fetched once into the agent's corepack cache (signature-checked by corepack); projects that pin
# another version via packageManager download it on first use.
USER agent
RUN COREPACK_ENABLE_DOWNLOAD_PROMPT=0 corepack install -g "pnpm@${PNPM_VERSION}" \
 && pnpm --version
USER root

COPY --chmod=0644 sshd_config /etc/ssh/sshd_config
COPY --chmod=0644 gitconfig /etc/gitconfig
COPY --chmod=0644 profile.sh /etc/profile.d/aios.sh
COPY --chown=agent:agent --chmod=0644 agent-profile.sh /home/agent/.profile
COPY --chmod=0755 entrypoint.sh /usr/local/sbin/aios-sandbox-entrypoint
COPY --chmod=0755 lib/aios/ /usr/local/lib/aios/
COPY --chmod=0755 bin/ /usr/local/bin/

EXPOSE 22
WORKDIR /workspace
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 CMD ["/usr/local/bin/aios-healthcheck"]
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/sbin/aios-sandbox-entrypoint"]
