# litellm
RAM: About 700–1,200 MB for LiteLLM alone, running 1 worker with `--max_requests_before_restart`. That figure comes from community reports of roughly 800 MiB idle; under sustained load it drifts upward until the worker is recycled. Cap the container with `mem_limit` at 1.5 GB on this laptop. The official production floor is 4 GiB per pod because Prisma memory keeps ratcheting up, but that's sized for heavy traffic. Postgres adds about 100–300 MB if it isn't shared, and Redis about 10–50 MB. Ollama running qwen3:4b Q4_K_M uses about 2.5–3.5 GB on the host while the model is loaded and is freed after `keep_alive` expires.

LITELLM PROXY, CHECKED 2026-10-07 AGAINST PRIMARY SOURCES

1) Image and tag
- Latest stable release is v1.104.0 (Oct 3, 2026). There is also v1.103.3, and these pre-releases: v1.105.0-rc.1 (Oct 4) and v1.106.0-dev.1 (Oct 7). Tags no longer use the `main-v…-stable` form; they are plain `vX.Y.Z`. The moving tags `main-stable`, `main-latest` and `dev` still exist.
- Recommended image: `ghcr.io/berriai/litellm:v1.104.0`. The official mirror is `docker.litellm.ai/berriai/litellm`. The docs say to pin a version tag and not use `latest` or `main-stable`. Images are cosign-signed.
- `ghcr.io/berriai/litellm-database` and `litellm-non_root` are still published (litellm-database got v1.106.0-dev.1 today). However, the current docs and both official compose files (repo root `docker-compose.yml` and `docker/docker-compose.quickstart.yml`) use the main `litellm` image with `DATABASE_URL`. The main image handles Postgres/Prisma, so the separate `-database` variant isn't needed.
- Breaking change in v1.104.0: the proxy refuses to start if the master key is unset, empty or a publicly known value such as `sk-1234`. Generate keys with `openssl rand -hex 32` and add an `sk-` prefix.

2) Required environment variables
- `LITELLM_MASTER_KEY=sk-…`: the admin key.
- `LITELLM_SALT_KEY=sk-…`: encrypts the provider credentials stored in the DB. It can never be rotated once models or keys have been added, so back it up.
- `DATABASE_URL=postgresql://litellm:pw@postgres:5432/litellm`
- Optional: `STORE_MODEL_IN_DB=True` (lets you manage models in the UI), `LITELLM_LOG=ERROR`, `LITELLM_MODE=PRODUCTION`, `NUM_WORKERS`, and the `REDIS_HOST`, `REDIS_PORT`, `REDIS_PASSWORD` trio. Use these individual Redis variables, not `REDIS_URL`; the docs flag a performance issue with the URL form.
- Provider keys: `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`, `OPENAI_API_KEY`, `MOONSHOT_API_KEY` (LiteLLM prefix `moonshot/`, default base `https://api.moonshot.ai/v1`, or `MOONSHOT_API_BASE`), `DEEPSEEK_API_KEY` (prefix `deepseek/`, base `https://api.deepseek.com`).

3) Healthchecks
- No auth needed: `/health/liveliness` (or `/health/liveness`) checks the process. `/health/readiness` also checks the DB connection.
- Auth needed: `/health` makes real calls to every model, which costs money, so don't use it as a container probe.
- For the Docker healthcheck, copy the repo's compose and use python urllib against `/health/liveliness`. I didn't check whether curl is in the image.

4) Config features
- `router_settings` supports `num_retries` (default 2), `timeout`, `allowed_fails` (default 3), `cooldown_time`, `fallbacks`, `context_window_fallbacks`, `content_policy_fallbacks`, `default_fallbacks`, `retry_policy` (fields such as `RateLimitErrorRetries`, `TimeoutErrorRetries`, `DefaultRetries`), `allowed_fails_policy`, and priority ordering with `order:` inside `litellm_params`.
- Budgets need Postgres. All budgets are enforced against spend read from the DB. Options:
  - a global `litellm_settings.max_budget` with `budget_duration` (`30d` resets on the 1st of the month, `24h` at UTC midnight)
  - a per-key `max_budget` set via `/key/generate`
  - team budgets via `/team/new`
  - per-model budgets on a key (`model_max_budget`), which is Enterprise-only
- Spend tracking uses LiteLLM's cost map. By default it fetches the current map from GitHub at startup (`LITELLM_MODEL_COST_MAP_URL`); setting `LITELLM_LOCAL_MODEL_COST_MAP=True` uses the copy bundled with the release.
- Langfuse: use the `langfuse_otel` callback (`litellm_settings.callbacks: ["langfuse_otel"]`). It's what Langfuse's own docs recommend.
  - It reads `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY`, plus `LANGFUSE_OTEL_HOST` (or, failing that, `LANGFUSE_HOST`). It appends `/api/public/otel` to the host and sends the header `x-langfuse-ingestion-version: 4`, which I confirmed in the v1.104.0 source.
  - For self-hosting, set `LANGFUSE_OTEL_HOST=http://langfuse-web:3000`.
  - The older `success_callback: ["langfuse"]` still works and uses the Langfuse Python SDK.
- Redis: `litellm_settings.cache: true` with `cache_params.type: redis` handles response caching. `router_settings.redis_host`, `redis_port` and `redis_password` share rate-limit and cooldown state, which only matters with more than one instance. If Hermes or other services share the same Redis, give LiteLLM its own logical DB or namespace.
- Ollama: use the `ollama_chat/` prefix (it calls `/api/chat`; this is the recommended prefix). `keep_alive` is supported. On Linux, the `host.docker.internal` hostname only resolves inside the container if you add `extra_hosts: ["host.docker.internal:host-gateway"]`. Ollama on the host also has to listen on a non-loopback address, e.g. `OLLAMA_HOST=0.0.0.0:11434` set through a systemd override. The default bind is 127.0.0.1, which the container can't reach.

5) Model IDs and prices (USD per million tokens, input/output). Checked against the LiteLLM cost map (main and v1.104.0), the live OpenRouter `/api/v1/models` endpoint, the Kimi and DeepSeek pricing pages, and the Anthropic model table.
- Anthropic direct:
  - `anthropic/claude-sonnet-5-5`: $2/$10, 1M context
  - `anthropic/claude-opus-5-5`: $4/$20, 1M context
  - `anthropic/claude-fable-5-1`: $10/$50, 1M context
  - All three exist. Claude Opus 5.5 and Claude Fable 5.1 are in the v1.104.0 cost map. Claude Sonnet 5.5 is only in v1.105.0-rc.1 and main, so on v1.104.0 its cost comes from the remotely fetched map. The sample config also sets it explicitly in `model_info`.
- OpenRouter equivalents: `openrouter/anthropic/claude-sonnet-5.5`, `claude-opus-5.5` and `claude-fable-5.1` (note the dots, not dashes), same prices. There are also `~anthropic/claude-*-latest` aliases.
- Kimi:
  - The latest coding model is Kimi K2.7 Code: `moonshot/kimi-k2.7-code`, $0.95/$4 (cache hit $0.19), 262K context. A `kimi-k2.7-code-highspeed` variant costs $1.90/$8.
  - Kimi K3 (flagship since Jul 16, 2026): `moonshot/kimi-k3`, $3/$15 (cache hit $0.30), 1M context.
  - On OpenRouter: `openrouter/moonshotai/kimi-k2.7-code` lists at $0.67/$3.35 and `openrouter/moonshotai/kimi-k3` at $0.50/$15. That OpenRouter input price for K3 looks odd; see the risks list.
- DeepSeek direct:
  - `deepseek/deepseek-v4-pro`: $1.32/$3.96 at peak, half price off-peak, 1M context, 384K output
  - `deepseek/deepseek-flash`: $0.30/$1.20 at peak, $0.15/$0.60 off-peak. This now serves DeepSeek-V4.1-Flash.
  - `deepseek-v4-flash` is a retired legacy alias that is still accepted. `deepseek-chat` and `deepseek-reasoner` have a deprecation date of 2026-07-24 in LiteLLM's map; don't use them.
  - Peak hours are 01:00–04:00 and 06:00–10:00 UTC on weekdays.
- DeepSeek on OpenRouter:
  - `openrouter/deepseek/deepseek-v4-pro-0813`: $0.66/$1.98
  - `openrouter/deepseek/deepseek-v4.1-flash`: $0.30/$1.20
  - `openrouter/deepseek/deepseek-v4-flash`: $0.03/$1.28
- Cheap Qwen on OpenRouter:
  - `openrouter/qwen/qwen3.7-flash`: $0.03/$0.13, 1M context. Cheapest current option.
  - `openrouter/qwen/qwen3.8-flash`: $0.15/$0.47. Newer.
  - Several older qwen3 OpenRouter entries have a LiteLLM deprecation date of 2026-10-09.
- Local: `ollama_chat/qwen3:4b`. The tag exists: 2.5 GB, Q4_K_M, 256K context. `qwen3:4b-instruct-2507-q4_K_M` (non-thinking) and `qwen3:4b-thinking-2507` also exist. Cost: $0.

6) Claude-specific traps
- Claude Opus 5.5, Claude Sonnet 5.5 and Claude Fable 5.1 return a 400 error for:
  - disabled thinking (or `budget_tokens`)
  - a forced `tool_choice` of `any` or a named tool
  - non-default `temperature`/`top_p`
- LiteLLM turns `reasoning_effort: "none"` into disabled thinking, so that also fails. Set `drop_params: true`, and keep Hermes from sending forced `tool_choice`, `temperature` or `reasoning_effort=none` to Claude.
- Claude Opus 5.5's default effort is `medium`.

## CMDS
# ---- generate secrets (run once, keep .env safe; SALT must never change) ----
printf 'LITELLM_MASTER_KEY=sk-%s\nLITELLM_SALT_KEY=sk-%s\n' "$(openssl rand -hex 32)" "$(openssl rand -hex 32)" >> .env

# ---- docker-compose.yml fragment ----
services:
  litellm:
    image: ghcr.io/berriai/litellm:v1.104.0
    command: ["--config=/app/config.yaml", "--port=4000", "--num_workers=1", "--max_requests_before_restart=10000"]
    volumes: ["./litellm/config.yaml:/app/config.yaml:ro"]
    ports: ["127.0.0.1:4000:4000"]
    extra_hosts: ["host.docker.internal:host-gateway"]   # required on Linux for Ollama on host
    env_file: [.env]
    environment:
      DATABASE_URL: postgresql://litellm:${LITELLM_DB_PASSWORD}@postgres:5432/litellm
      STORE_MODEL_IN_DB: "True"
      LITELLM_LOG: ERROR
      LITELLM_MODE: PRODUCTION
      REDIS_HOST: redis
      REDIS_PORT: "6379"
      LANGFUSE_OTEL_HOST: http://langfuse-web:3000
      # LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / provider keys come from .env
    depends_on:
      postgres: {condition: service_healthy}
      redis: {condition: service_started}
    mem_limit: 1536m
    healthcheck:
      test: ["CMD-SHELL", "python3 -c \"import urllib.request; urllib.request.urlopen('http://localhost:4000/health/liveliness')\""]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 60s
    restart: unless-stopped

# Host side (user runs with sudo): make Ollama reachable from containers
#   sudo systemctl edit ollama  ->  [Service]\nEnvironment="OLLAMA_HOST=0.0.0.0:11434"
#   (then firewall 11434 to docker bridge/localhost only)

# ---- litellm/config.yaml ----
model_list:
  # Anthropic direct
  - model_name: claude-sonnet
    litellm_params: {model: anthropic/claude-sonnet-5-5, api_key: os.environ/ANTHROPIC_API_KEY}
    model_info: {input_cost_per_token: 0.000002, output_cost_per_token: 0.00001}  # not in v1.104.0 map
  - model_name: claude-opus
    litellm_params: {model: anthropic/claude-opus-5-5, api_key: os.environ/ANTHROPIC_API_KEY}
  - model_name: claude-fable
    litellm_params: {model: anthropic/claude-fable-5-1, api_key: os.environ/ANTHROPIC_API_KEY}
  # Same aliases via OpenRouter as lower-priority deployments (order 2)
  - model_name: claude-sonnet
    litellm_params: {model: openrouter/anthropic/claude-sonnet-5.5, api_key: os.environ/OPENROUTER_API_KEY, order: 2}
  - model_name: claude-opus
    litellm_params: {model: openrouter/anthropic/claude-opus-5.5, api_key: os.environ/OPENROUTER_API_KEY, order: 2}
  # Kimi (Moonshot direct, OpenRouter backup)
  - model_name: kimi-code
    litellm_params: {model: moonshot/kimi-k2.7-code, api_key: os.environ/MOONSHOT_API_KEY, api_base: "https://api.moonshot.ai/v1"}
  - model_name: kimi-code
    litellm_params: {model: openrouter/moonshotai/kimi-k2.7-code, api_key: os.environ/OPENROUTER_API_KEY, order: 2}
  - model_name: kimi-k3
    litellm_params: {model: moonshot/kimi-k3, api_key: os.environ/MOONSHOT_API_KEY, api_base: "https://api.moonshot.ai/v1"}
  - model_name: kimi-k3
    litellm_params: {model: openrouter/moonshotai/kimi-k3, api_key: os.environ/OPENROUTER_API_KEY, order: 2}
  # DeepSeek direct, OpenRouter backup
  - model_name: deepseek-pro
    litellm_params: {model: deepseek/deepseek-v4-pro, api_key: os.environ/DEEPSEEK_API_KEY}
  - model_name: deepseek-pro
    litellm_params: {model: openrouter/deepseek/deepseek-v4-pro-0813, api_key: os.environ/OPENROUTER_API_KEY, order: 2}
  - model_name: deepseek-flash
    litellm_params: {model: deepseek/deepseek-flash, api_key: os.environ/DEEPSEEK_API_KEY}
  - model_name: deepseek-flash
    litellm_params: {model: openrouter/deepseek/deepseek-v4.1-flash, api_key: os.environ/OPENROUTER_API_KEY, order: 2}
  # Cheap Qwen (OpenRouter)
  - model_name: qwen-cheap
    litellm_params: {model: openrouter/qwen/qwen3.7-flash, api_key: os.environ/OPENROUTER_API_KEY}
  # Local Ollama on host
  - model_name: local-qwen
    litellm_params:
      model: ollama_chat/qwen3:4b
      api_base: http://host.docker.internal:11434
      keep_alive: "10m"
      timeout: 300
    model_info: {input_cost_per_token: 0, output_cost_per_token: 0, supports_function_calling: true}

litellm_settings:
  drop_params: true                 # strip params a provider rejects
  request_timeout: 600
  num_retries: 2
  callbacks: ["langfuse_otel"]      # Langfuse via OTEL -> ${LANGFUSE_OTEL_HOST}/api/public/otel
  cache: true
  cache_params:
    type: redis
    host: os.environ/REDIS_HOST
    port: os.environ/REDIS_PORT
    password: os.environ/REDIS_PASSWORD
    namespace: litellm
    ttl: 3600
    supported_call_types: ["acompletion", "completion", "aembedding", "embedding"]
  max_budget: 50                    # USD global cap
  budget_duration: 30d
  json_logs: true

router_settings:
  routing_strategy: simple-shuffle
  num_retries: 2
  timeout: 600
  allowed_fails: 3
  cooldown_time: 30
  enable_pre_call_checks: true
  retry_policy:
    RateLimitErrorRetries: 3
    TimeoutErrorRetries: 2
    InternalServerErrorRetries: 2
    AuthenticationErrorRetries: 0
    BadRequestErrorRetries: 0
    ContentPolicyViolationErrorRetries: 0
    DefaultRetries: 1
  fallbacks:
    - {"claude-opus": ["claude-sonnet", "kimi-k3"]}
    - {"claude-fable": ["claude-opus"]}
    - {"claude-sonnet": ["kimi-code", "deepseek-pro"]}
    - {"kimi-code": ["deepseek-pro", "claude-sonnet"]}
    - {"kimi-k3": ["claude-opus"]}
    - {"deepseek-pro": ["kimi-code"]}
    - {"deepseek-flash": ["qwen-cheap", "local-qwen"]}
    - {"qwen-cheap": ["deepseek-flash", "local-qwen"]}
  context_window_fallbacks:
    - {"kimi-code": ["kimi-k3"]}
    - {"local-qwen": ["qwen-cheap"]}
  # single instance: redis_host/port/password here only needed for multi-replica rate-limit sharing

general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
  database_url: os.environ/DATABASE_URL
  store_model_in_db: true
  proxy_batch_write_at: 60
  database_connection_pool_limit: 5
  allow_requests_on_db_unavailable: true
  disable_error_logs: true

# ---- smoke tests ----
curl -s http://localhost:4000/health/liveliness
curl -s http://localhost:4000/health/readiness
curl -s http://localhost:4000/v1/chat/completions -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H 'Content-Type: application/json' -d '{"model":"local-qwen","messages":[{"role":"user","content":"ping"}]}'
# per-agent virtual key with budget:
curl -s http://localhost:4000/key/generate -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H 'Content-Type: application/json' -d '{"key_alias":"hermes","max_budget":20,"budget_duration":"30d","models":["claude-sonnet","kimi-code","deepseek-flash","qwen-cheap","local-qwen"]}'

## RISKS
- Claude Sonnet 5.5 (`claude-sonnet-5-5` / `openrouter/anthropic/claude-sonnet-5.5`) is missing from the cost map bundled with LiteLLM v1.104.0; it only appears in v1.105.0-rc.1 and main. Requests should still pass through, and the remote cost map is fetched at startup by default, but drop_params handling and capability flags for Sonnet 5.5 may be incomplete until v1.105.0 is stable. The sample config sets explicit model_info pricing as a stopgap.
- Claude Opus 5.5, Claude Sonnet 5.5 and Claude Fable 5.1 return a 400 for disabled thinking, budget_tokens, forced tool_choice (any or a named tool) and non-default temperature/top_p. LiteLLM maps reasoning_effort='none' to disabled thinking, and I could not confirm that drop_params strips all of these for these models on v1.104.0. Test Hermes's request shapes against each Claude alias.
- Some OpenRouter prices look inconsistent and may be promotional, lowest-provider or stale: kimi-k3 lists $0.50 input against Moonshot direct at $3.00, and plain deepseek/deepseek-v4-pro lists $0.21/$0.42 against $0.66/$1.98 for v4-pro-0813. LiteLLM's map has openrouter v4-pro-0813 at $1.32/$3.96. Treat LiteLLM spend figures for OpenRouter routes as estimates.
- DeepSeek pricing changes by time of day (off-peak is half price). LiteLLM's map uses peak prices, so recorded spend will overstate off-peak usage.
- Moonshot's docs now live at platform.kimi.ai, but every API reference I found still points to https://api.moonshot.ai/v1, which is also LiteLLM's default. I did not verify whether an api.kimi.ai host exists or is preferred.
- The RAM figures for LiteLLM come from community sources and the production doc's 4 GiB floor, not a measurement on this machine. Measure with `docker stats` after deployment.
- I did not confirm whether curl is included in the v1.104.0 image. The python urllib healthcheck copied from the official repo compose is the safe choice.
- Langfuse server version and OTEL compatibility: LiteLLM's langfuse_otel sends the header x-langfuse-ingestion-version: 4 to /api/public/otel. Check that the self-hosted Langfuse version chosen for the stack, v3 or v4, accepts it; the Langfuse research task should confirm. Fallback: `success_callback: ["langfuse"]` with LANGFUSE_HOST.
- On Linux, host.docker.internal needs the extra_hosts host-gateway entry, and Ollama must bind beyond 127.0.0.1 (OLLAMA_HOST=0.0.0.0). That needs a sudo systemd override, and port 11434 should be firewalled.
- LITELLM_SALT_KEY cannot be rotated after models or keys are stored in the DB, so losing .env makes the stored credentials unreadable.
- Per-model budgets on a key (model_max_budget) are an Enterprise feature. The global, per-key and team budgets are open source and require Postgres.
- Several older qwen3 OpenRouter IDs (e.g. qwen3-8b, qwen3-30b-a3b, qwen3-max) have a LiteLLM deprecation date of 2026-10-09, so avoid them; qwen3.7-flash and qwen3.8-flash are current.

## SOURCES
https://docs.litellm.ai/docs/proxy/deploy
https://docs.litellm.ai/docs/proxy/docker_quick_start
https://docs.litellm.ai/docs/proxy/prod
https://docs.litellm.ai/docs/proxy/health
https://docs.litellm.ai/docs/proxy/reliability
https://docs.litellm.ai/docs/routing
https://docs.litellm.ai/docs/proxy/config_settings
https://docs.litellm.ai/docs/proxy/users
https://docs.litellm.ai/docs/proxy/caching
https://docs.litellm.ai/docs/proxy/logging
https://docs.litellm.ai/docs/providers/ollama
https://docs.litellm.ai/docs/providers/moonshot
https://docs.litellm.ai/docs/providers/anthropic
https://github.com/BerriAI/litellm/releases
https://github.com/BerriAI/litellm/releases/tag/v1.104.0
https://github.com/BerriAI/litellm/pkgs/container/litellm-database
https://raw.githubusercontent.com/BerriAI/litellm/main/docker-compose.yml
https://raw.githubusercontent.com/BerriAI/litellm/main/docker/docker-compose.quickstart.yml
https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json
https://raw.githubusercontent.com/BerriAI/litellm/v1.104.0/model_prices_and_context_window.json
https://raw.githubusercontent.com/BerriAI/litellm/v1.105.0-rc.1/model_prices_and_context_window.json
https://raw.githubusercontent.com/BerriAI/litellm/v1.104.0/litellm/integrations/langfuse/langfuse_otel.py
https://langfuse.com/integrations/gateways/litellm
https://openrouter.ai/api/v1/models
https://platform.kimi.ai/docs/pricing/chat
https://api-docs.deepseek.com/quick_start/pricing
https://ollama.com/library/qwen3/tags
https://gitlab.com/psyb0t/aigate/-/tags/v3.14.8
claude-api skill model table (cached 2026-09-25): claude-opus-5-5 $4/$20, claude-sonnet-5-5 $2/$10, claude-fable-5-1 $10/$50