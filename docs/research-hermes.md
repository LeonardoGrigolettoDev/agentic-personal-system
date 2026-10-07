# Hermes Agent (Nous Research) – agent runtime
RAM: About 350 MB idle for gateway+API+dashboard (third-party measurement), growing 30–100 MB/h under load. Budget a 1.0–1.5 GB container limit for this laptop without browser tools, or 2 GB with browser tools or several subagents. The docs say 1 GB minimum and 2–4 GB recommended. A docker terminal-backend sandbox container adds memory on top (whatever you set in container_memory). Image size is about 0.93 GB compressed, so expect about 2.5–3 GB on disk.

LATEST VERSION (verified 2026-10-07): GitHub release "Hermes Agent v0.21.5 (v2026.9.24)", git tag v2026.9.24, published 2026-09-24. It rolls up about 460 PRs merged since v0.21.4. Docker images are built from that tag as nousresearch/hermes-agent:v2026.9.24. Docker Hub tags: latest/stable (about 925 MB compressed, amd64+arm64), v2026.9.24, matching *-desktop variants (about 1.28 GB), and main/main-desktop (nightly). PyPI "hermes-agent" lags at 0.19.0 (2026-07-20, Python 3.11 to <3.14), so don't use pip for current features. License is MIT.

INSTALL METHODS: (1) official one-liner `curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash`. It installs source into ~/.hermes/hermes-agent/, the CLI into ~/.local/bin/hermes and data into ~/.hermes/. It ships its own Python 3.14 toolchain, so system Python 3.12 is fine. (2) Docker: image nousresearch/hermes-agent. Code lives in /opt/hermes and state in /opt/data (HERMES_HOME=/opt/data). It runs as non-root user `hermes` (UID 10000), remapped via HERMES_UID/HERMES_GID (the docs also mention PUID/PGID). s6-overlay /init is PID 1, so don't override the entrypoint. The Docker CLI is included in the image. (3) pip install hermes-agent (stale). No npm/uv-tool path is documented. Upgrade: `hermes update` for source installs, or pull the image and recreate the container (config is auto-migrated with timestamped backups).

CONFIG: everything lives in HERMES_HOME (~/.hermes or /opt/data):
- config.yaml: non-secret settings. ${VAR} substitution comes from .env.
- .env: secrets.
- SOUL.md: agent identity.
- memories/ (MEMORY.md, USER.md)
- skills/
- sessions/
- state.db: SQLite WAL + FTS5 session search.
- cron/jobs.json and cron/output/
- hooks/
- logs/: auto-redacted.
CLI: `hermes config get|set|edit|check|migrate`, `hermes setup`, `hermes model`, `hermes doctor`, `hermes -z` (scripted one-shot), `hermes chat -q`, `hermes sessions`, `hermes profile create X` (isolated profiles, each needs its own API_SERVER_PORT).

LITELLM / OPENAI-COMPATIBLE: use `model.provider: custom` with `model.base_url` and `model.default`, plus `api_key` or `key_env`. OPENAI_BASE_URL is honored only by the openai-api provider; LLM_MODEL was removed. Named providers can go under `providers:` with keys api, key_env, api_key, key_cmd, transport (chat_completions|anthropic_messages|codex_responses), default_model, context_length, extra_body, extra_headers. Switch in a session with `/model custom:<name>:<model>`. The docs explicitly cite LiteLLM at http://host:4000/v1. Context length resolution order: model.context_length (pinned), then the endpoint's /v1/models, then models.dev, then a 128K fallback. HARD GATE: Hermes rejects models whose context is under 64,000 tokens. You can override this by pinning context_length, but that's a known pain point (issues #53347, #132303). Separate model routing exists for auxiliary.compression (model/base_url) and for delegation (model, provider, base_url, api_key, api_mode).

FEATURES:
- Sessions: SQLite state.db with FTS5 and a session_search tool. Use /new or /reset in chats.
- Memory: bounded MEMORY.md (about 2,200 chars) and USER.md (about 1,375 chars), injected as a frozen snapshot at session start. Config keys: memory.memory_enabled, user_profile_enabled, memory_char_limit, write_approval. External providers (Honcho, Hindsight, Supermemory, Mem0, OpenViking) are plugins. No native Postgres/pgvector memory; you'd integrate it via a pre_llm_call plugin hook (RAG pattern documented) or MCP.
- Cron: `hermes cron create "every 2h" "prompt"`, `/cron add ... --skill X`, or natural language. Schedules accept "in 30m", "every 1d", "weekdays at 9am", 5-field cron, or ISO timestamps. --no-agent --script runs a script with no LLM. Delivery targets: telegram, discord, local, origin, all, etc. The gateway ticks the scheduler every 60s, so the gateway must be running. Config keys: cron.model, cron.catch_up_missed, cron.max_parallel_jobs.
- Skills: ~/.hermes/skills/<category>/<name>/SKILL.md plus references/, templates/, scripts/. Frontmatter: name, description, version, platforms, metadata.hermes{tags, category, requires_toolsets, fallback_for_toolsets, config}. Loaded by progressive disclosure (skills_list, then skill_view). The agent self-authors skills via skill_manage. Config: skills.external_dirs, create_dir, write_approval, project_discovery. `hermes skills install <source>` runs a security scan.
- Hooks (4 systems):
  - Gateway hooks: ~/.hermes/hooks/<name>/HOOK.yaml + handler.py with handle(event_type, context). Events: gateway:startup, session:start|end|reset|compress, agent:start|step|end, command:*, reaction:*.
  - Plugin hooks: ctx.register_hook. Events: pre_tool_call (can return block/approve/modify), post_tool_call, transform_tool_result, pre_llm_call (returns {"context": ...}), post_llm_call, on_session_start/end/finalize/reset, subagent_start/stop, pre_gateway_dispatch, on_stream_*, pre_transcription, and others.
  - Shell hooks: `hooks:` list in config.yaml with event, command, matcher regex, timeout (default 60, max 300) and fail_closed. They read stdin JSON {hook_event_name, tool_name, tool_input, session_id, cwd, profile, extra}. Exit code 2 blocks. They need consent via --accept-hooks, HERMES_ACCEPT_HOOKS=1, hooks_auto_accept: true, or ~/.hermes/shell-hooks-allowlist.json.
  - Outbound webhooks: hooks.outbound [{url, events, secret_env, timeout}], signed with X-Hermes-Signature-256 HMAC.
  - Plugin hook timeout: plugins.hook_callback_timeout (default 30s).
- Subagents: delegate_task tool. delegation.max_concurrent_children defaults to 10, max_spawn_depth to 1, max_iterations to 250. Leaf children can't use delegate_task, clarify, memory, send_message or cronjob.
- Sandboxing: terminal.backend can be local, docker, ssh, singularity, modal, daytona or vercel_sandbox. The docker backend uses one long-lived container via docker exec, with docker_image, container_cpu, container_memory, container_persistent, docker_volumes and docker_forward_env. Running Hermes in Docker with the docker backend requires mounting /var/run/docker.sock. The ssh backend uses TERMINAL_SSH_HOST/USER/PORT. Approvals are set by approvals.mode.
- Messaging: 30+ platforms (Telegram, Discord, Slack, WhatsApp, Signal, Email, Teams, Matrix and others). Access is deny-by-default via allowlists (TELEGRAM_BOT_TOKEN plus TELEGRAM_ALLOWED_USERS, DISCORD_ALLOWED_USERS) or DM pairing (`hermes pairing approve`).

LONG-LIVED SERVER: `hermes gateway run` is the daemon (messaging, cron and API server). The OpenAI-compatible API server is enabled with API_SERVER_ENABLED=true and API_SERVER_KEY (at least 8 chars, mandatory). API_SERVER_HOST defaults to 127.0.0.1 and must be 0.0.0.0 in a container. Port is 8642 (API_SERVER_PORT). Endpoints: /v1/chat/completions, /v1/responses, /v1/runs, /v1/models, /api/sessions/{id}/chat, /health, /health/detailed. CORS is set with API_SERVER_CORS_ORIGINS. The dashboard runs on 9119 when HERMES_DASHBOARD=1 and needs auth (HERMES_DASHBOARD_BASIC_AUTH_USERNAME/_PASSWORD or OIDC). The official compose file uses network_mode: host. For our stack, use a bridge network so `litellm` resolves. The API gives full terminal access, so never expose it publicly.

OBSERVABILITY: Langfuse is a bundled, opt-in plugin, enabled with `hermes plugins enable observability/langfuse` or `plugins.enabled: [observability/langfuse]`. Env: HERMES_LANGFUSE_PUBLIC_KEY, HERMES_LANGFUSE_SECRET_KEY, HERMES_LANGFUSE_BASE_URL (self-hosted OK; the standard LANGFUSE_* names are also accepted). It traces one span per turn, one generation per LLM call, and tool observations, and it fails open. OpenTelemetry comes from the third-party plugin briancaffey/hermes-otel (OTLP HTTP), not first-party. Alternative or complement: LiteLLM's own Langfuse callback captures every LLM call at the gateway.

RAM: the docs give 1 GB minimum and 2–4 GB recommended (2 GB or more with browser tools; add --shm-size=1g). A third-party measurement found about 330 MB idle and about 365 MB at boot for gateway+API+dashboard, growing 30–100 MB/h under load (a known leak pattern; restart periodically). The image is about 925 MB compressed.

## CMDS
# --- docker-compose.yml fragment (bridge network, LiteLLM by service name) ---
services:
  hermes:
    image: nousresearch/hermes-agent:v2026.9.24   # pin; 'latest' also OK
    container_name: hermes
    restart: unless-stopped
    command: ["gateway", "run"]
    init: false            # image already uses s6 /init as PID 1; do NOT override entrypoint
    depends_on: [litellm]
    ports:
      - "127.0.0.1:8642:8642"   # OpenAI-compatible agent API
      # - "127.0.0.1:9119:9119" # dashboard (set HERMES_DASHBOARD=1 + basic auth)
    volumes:
      - ./data/hermes:/opt/data
      # - /var/run/docker.sock:/var/run/docker.sock   # only if terminal.backend: docker
    environment:
      HERMES_UID: "1000"
      HERMES_GID: "1000"
      API_SERVER_ENABLED: "true"
      API_SERVER_HOST: "0.0.0.0"
      API_SERVER_PORT: "8642"
      API_SERVER_KEY: ${HERMES_API_KEY}
      LITELLM_API_KEY: ${LITELLM_MASTER_KEY}
      HERMES_LANGFUSE_PUBLIC_KEY: ${LANGFUSE_PUBLIC_KEY}
      HERMES_LANGFUSE_SECRET_KEY: ${LANGFUSE_SECRET_KEY}
      HERMES_LANGFUSE_BASE_URL: http://langfuse-web:3000
    deploy:
      resources:
        limits: { memory: 2G, cpus: "2.0" }

# --- ./data/hermes/config.yaml (minimal) ---
model:
  provider: custom
  base_url: http://litellm:4000/v1
  key_env: LITELLM_API_KEY
  default: claude-sonnet          # must equal a model_name alias in LiteLLM config
  context_length: 200000          # pin; Hermes rejects <64000
providers:                        # optional named routes, switch with /model custom:litellm:<alias>
  litellm:
    api: http://litellm:4000/v1
    key_env: LITELLM_API_KEY
    transport: chat_completions
    default_model: claude-sonnet
auxiliary:
  compression:
    model: deepseek-chat          # cheap LiteLLM alias
    base_url: http://litellm:4000/v1
delegation:
  model: kimi-k2                  # LiteLLM alias for subagents
  base_url: http://litellm:4000/v1
  api_key: ${LITELLM_API_KEY}
  max_concurrent_children: 3      # keep low on 13 GiB laptop
terminal:
  backend: local                  # inside the container = already isolated; use docker+socket for stronger sandbox
memory:
  memory_enabled: true
  user_profile_enabled: true
skills:
  external_dirs: [/opt/data/shared-skills]
plugins:
  enabled: [observability/langfuse]
hooks_auto_accept: false
hooks:
  - event: pre_tool_call
    matcher: "terminal"
    command: /opt/data/hooks-bin/decision-guard.sh   # could call Decision Service over HTTP
    timeout: 10
    fail_closed: true
  outbound:
    - url: http://decision-service:8080/hermes-events
      events: [agent:end, session:reset]
      secret_env: HERMES_WEBHOOK_SECRET
      timeout: 5

# --- first-run / ops commands ---
mkdir -p ./data/hermes
docker compose run --rm hermes setup          # interactive wizard (optional if config.yaml prewritten)
docker compose up -d hermes
curl -s localhost:8642/health
curl -s localhost:8642/v1/chat/completions -H "Authorization: Bearer $HERMES_API_KEY" -H 'Content-Type: application/json' -d '{"model":"hermes-agent","messages":[{"role":"user","content":"ping"}]}'
docker exec -it hermes hermes doctor
docker exec -it hermes hermes plugins list
docker exec -it hermes hermes cron create "weekdays at 9am" "Summarize yesterday's notes"
docker exec -it hermes hermes chat            # interactive TUI inside container
# host-native alternative:
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash && source ~/.bashrc && hermes setup

## RISKS
- 64K minimum-context hard gate: Qwen3 4B on Ollama (32K native) is rejected unless you pin context_length >= 64000 (open issues #53347, #132303). At a real 64K, the fp16 KV cache for Qwen3-4B is around 9 GB (my estimate; about half with q8 KV), which is not feasible on this machine. Use Qwen3 4B only for auxiliary/cheap side tasks, or not at all inside Hermes, and use cloud models via LiteLLM as the main model.
- PyPI hermes-agent (0.19.0) lags far behind GitHub v0.21.5. Use the Docker image or install.sh, not pip.
- The exact model id the Hermes API server exposes on /v1/models (I used 'hermes-agent' in the curl example) is unverified. Check GET /v1/models.
- The image tag v2026.9.24 is from Docker Hub and the release notes. Whether the image tag carries a leading 'v' should be confirmed with docker pull.
- I did not verify that the langfuse Python SDK is preinstalled in the Docker image. If it's missing, the plugin silently no-ops (it fails open). You may need a thin derived image (FROM nousresearch/hermes-agent, then /opt/hermes/.venv/bin/pip install langfuse). LiteLLM's own Langfuse callback is a reliable fallback for LLM-call tracing.
- OpenTelemetry support is a third-party plugin (briancaffey/hermes-otel), not first-party.
- Shell-hook field names (matcher, fail_closed, hooks_auto_accept) and the hooks.outbound nesting come from the docs page via a summarizer. Combining a list under `hooks:` with `hooks.outbound:` in the same YAML key may conflict. Check the exact schema with `hermes config check` before relying on it.
- The idle RAM figure (about 330 MB) comes from a third-party blog (lumadock / fast.io), not official docs. Gateway memory growth and OOM is a known reported issue.
- Mounting /var/run/docker.sock into the Hermes container (needed for terminal.backend: docker) gives it root-equivalent control of the host. Prefer backend local inside the container (already isolated by the container) or the ssh backend.
- The API server grants full terminal/tool access. Bind it only to 127.0.0.1 or the internal network, and always set API_SERVER_KEY.
- The official compose file uses network_mode: host. Bridge networking with API_SERVER_HOST=0.0.0.0 is my adaptation, and I have not tested it.
- On Railway, the s6-overlay PID-1 and /opt/data volume requirements need a persistent volume. A Railway template 'hermes-agent-nousresearch' exists but I did not inspect it.

## SOURCES
https://hermes-agent.nousresearch.com/docs/
https://hermes-agent.nousresearch.com/docs/getting-started/installation
https://hermes-agent.nousresearch.com/docs/user-guide/configuration
https://hermes-agent.nousresearch.com/docs/user-guide/docker
https://hermes-agent.nousresearch.com/docs/integrations/providers
https://hermes-agent.nousresearch.com/docs/user-guide/features/hooks/
https://hermes-agent.nousresearch.com/docs/user-guide/features/skills
https://hermes-agent.nousresearch.com/docs/user-guide/features/cron
https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server
https://hermes-agent.nousresearch.com/docs/user-guide/features/memory
https://hermes-agent.nousresearch.com/docs/user-guide/features/delegation
https://hermes-agent.nousresearch.com/docs/user-guide/features/tools
https://hermes-agent.nousresearch.com/docs/user-guide/messaging/
https://hermes-agent.nousresearch.com/docs/reference/cli-commands
https://github.com/NousResearch/hermes-agent
https://github.com/NousResearch/hermes-agent/releases/latest
https://raw.githubusercontent.com/NousResearch/hermes-agent/main/docker-compose.yml
https://raw.githubusercontent.com/NousResearch/hermes-agent/main/Dockerfile
https://hub.docker.com/r/nousresearch/hermes-agent/tags
https://pypi.org/project/hermes-agent/
https://langfuse.com/integrations/other/hermes
https://github.com/NousResearch/hermes-agent/issues/53347
https://github.com/NousResearch/hermes-agent/issues/132303
https://lumadock.com/tutorials/hermes-gateway-memory-leak-oom-fix
https://www.virtua.cloud/learn/en/tutorials/run-hermes-agent-docker-compose-vps
https://signoz.io/docs/hermes-monitoring