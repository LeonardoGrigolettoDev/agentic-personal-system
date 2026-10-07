# langfuse (observability) - self-host v4 vs Langfuse Cloud / Phoenix / Helicone / LiteLLM spend logs
RAM: Official minimums: about 25,600 MB (web 4G, worker 4G, PG 4G, Redis 1.5G, ClickHouse 8G, MinIO 4G); the VM guidance is 16 GiB. My unverified estimate for a tuned, capped single-user local setup is 1,600-2,500 MB: web 500-800, worker 300-500, ClickHouse 500-1,000 with merge spikes up to the 1,536 cap, MinIO about 150, plus about 100 incremental in shared Postgres/Redis. Langfuse Cloud Hobby adds 0 MB locally; Phoenix is probably 300-600 MB (unverified); LiteLLM spend logs add 0 extra services.

CURRENT STATE (verified 2026-10-07): Langfuse's current major version is v4. v4.0.0 GA shipped 2026-07-29; latest is v4.54.0 (2026-10-07), with near-daily releases. The official docker-compose.yml on main uses the images `docker.langfuse.com/langfuse/langfuse:4` (web) and `docker.langfuse.com/langfuse/langfuse-worker:4`. The same images are on Docker Hub as `langfuse/langfuse` and `langfuse/langfuse-worker`. The v3-to-v4 upgrade guide says: "The infrastructure architecture is unchanged... No new services are introduced". v4 adds full-text search, monitors/alerts, and Observations/Metrics API v2.

COMPONENTS (official compose):
- langfuse-web on :3000
- langfuse-worker on 127.0.0.1:3030
- clickhouse/clickhouse-server:25.12, on 8123 HTTP and 9000 TCP
- cgr.dev/chainguard/minio, on 9090 API and 9091 console
- redis:7 with `--maxmemory-policy noeviction`
- postgres:${POSTGRES_VERSION:-17}

v4 minimum versions:
- ClickHouse: 25.12 minimum, 26.4 recommended. These versions are needed for lightweight updates, the JSON type and full-text search.
- Postgres: 15 minimum, 16 recommended.
- Redis: 7.0 minimum, 7.2 recommended. Valkey must be 8 or newer.

Everything must run in UTC.

OFFICIAL SIZING:
- Docker Compose VM: "at least 4 cores and 16 GiB of memory" and about 100 GiB of disk.
- Minimums per component from the scaling page:
  - web: 2 CPU / 4 GiB
  - worker: 2 CPU / 4 GiB
  - Postgres: 2 CPU / 4 GiB
  - Redis: 1 CPU / 1.5 GiB
  - ClickHouse: 2 CPU / 8 GiB
  - MinIO: 2 CPU / 4 GiB

  That adds up to about 25 GiB of production minimums. ClickHouse's own docs recommend 32 GB+ and give a separate recipe for running under 16 GB.
- Node heap defaults to about 1.7 GiB unless you set `NODE_OPTIONS=--max-old-space-size`.
- Horizontal scaling is not supported in Compose.

SHARING POSTGRES: yes. Langfuse (Prisma) "uses the public schema in the selected database". Give it a dedicated database (`langfuse`) and role on our pgvector Postgres (pg16/17 meets the v4 minimum of 15). Use `DATABASE_URL=postgresql://langfuse:PW@postgres:5432/langfuse`. `DIRECT_URL` defaults to `DATABASE_URL`.

SHARING REDIS: yes, officially supported. There are two options:
- A db index via `REDIS_CONNECTION_STRING=redis://:PW@redis:6379/2`.
- `REDIS_KEY_PREFIX=langfuse:` (it must end with `:`). The source comment says it is used by BullMQ's native prefix and the ioredis keyPrefix, "Useful for multi-tenant Redis".

Caveat: the whole instance must use `maxmemory-policy noeviction`, so our app can't use that Redis as an LRU cache. If it needs one, run a second Redis. Sizing is about 1 GB of Redis per 100k events/min, which is negligible for us.

REQUIRED ENV:
- `NEXTAUTH_URL`
- `NEXTAUTH_SECRET` (`openssl rand -base64 32`; web only)
- `SALT` (`openssl rand -base64 32`)
- `ENCRYPTION_KEY` (64 hex characters, `openssl rand -hex 32`)
- `DATABASE_URL`
- `CLICKHOUSE_URL` (http://clickhouse:8123)
- `CLICKHOUSE_MIGRATION_URL` (clickhouse://clickhouse:9000)
- `CLICKHOUSE_USER` and `CLICKHOUSE_PASSWORD`
- `CLICKHOUSE_CLUSTER_ENABLED=false`. The code default is true, so this must be set for a single node.
- Redis: `REDIS_CONNECTION_STRING`, or `REDIS_HOST`/`REDIS_PORT`/`REDIS_AUTH`
- S3 event upload: `LANGFUSE_S3_EVENT_UPLOAD_{BUCKET,REGION,ACCESS_KEY_ID,SECRET_ACCESS_KEY,ENDPOINT,FORCE_PATH_STYLE,PREFIX}`. This is required.
- S3 media upload: `LANGFUSE_S3_MEDIA_UPLOAD_*`, the same set. On web, the media endpoint must be reachable from the browser; there is also `_INTERNAL_ENDPOINT`.
- `LANGFUSE_S3_BATCH_EXPORT_*` is optional.

Useful optional env: `TELEMETRY_ENABLED=false` and `AUTH_DISABLE_SIGNUP=true`.

Worker tuning:
- `LANGFUSE_INGESTION_QUEUE_PROCESSING_CONCURRENCY` (default 20)
- `LANGFUSE_TRACE_UPSERT_WORKER_CONCURRENCY` (default 25)
- `CLICKHOUSE_MAX_OPEN_CONNECTIONS` (default 25)

HEADLESS INIT (web container only):
- `LANGFUSE_INIT_ORG_ID`: required to init anything
- `LANGFUSE_INIT_ORG_NAME`
- `LANGFUSE_INIT_PROJECT_ID`
- `LANGFUSE_INIT_PROJECT_NAME`
- `LANGFUSE_INIT_PROJECT_RETENTION`: integer, at least 3 days per the zod schema
- `LANGFUSE_INIT_PROJECT_PUBLIC_KEY`: format pk-lf-...
- `LANGFUSE_INIT_PROJECT_SECRET_KEY`: format sk-lf-...
- `LANGFUSE_INIT_USER_EMAIL`, `LANGFUSE_INIT_USER_NAME`, `LANGFUSE_INIT_USER_PASSWORD`

These resources are created on startup if missing. Do not double-quote values in compose. Pre-generating the pk/sk lets LiteLLM and Hermes read the same keys from .env with no UI step.

LITELLM INTEGRATION: the recommended callback is `langfuse_otel`, which works with Langfuse v3 and v4. Env vars are `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, and `LANGFUSE_OTEL_HOST` or `LANGFUSE_HOST` (default https://cloud.langfuse.com). The legacy `langfuse` callback needs Python SDK v4 and Langfuse server 3.63 or newer. Moving from Cloud to self-hosted is therefore just a change to the host and keys.

FIT ON THIS LAPTOP (about 5 GB free): untuned, ClickHouse alone grows into GBs: a 5 GB mark cache and an 8 GB uncompressed cache by default. Third-party reports say it OOMs during background merges on small hosts; "8 GiB is sufficient for a single developer..." (ssdnodes guide, not official).

With the tuning below and capped containers, my unverified estimate for idle or light single-user load is:
- web: 0.5-0.8 GB
- worker: 0.3-0.5 GB
- ClickHouse: 0.5-1.0 GB, with spikes during merges and migrations
- MinIO: about 0.15 GB
- incremental shared Postgres and Redis: about 0.1 GB

That totals about 1.6-2.5 GB. It technically fits, but not alongside Ollama Qwen3-4B (about 3+ GB loaded), LiteLLM (0.3-0.6 GB) and Hermes inside 5 GB. Swap thrash or OOM kills are likely while Chrome and several Claude sessions are open.

ClickHouse low-memory knobs, from ClickHouse's "less than 16GB" tips and the Altinity KB:
- mark_cache 256 MB
- index_mark_cache 64 MB
- uncompressed_cache 16 MB
- `max_server_memory_usage_to_ram_ratio` 0.75 (respects the cgroup mem_limit)
- background_pool_size 2, with the matching merge_tree free-entries settings set to 2. Otherwise table creation can fail.
- remove query_thread_log, opentelemetry_span_log, processors_profile_log, trace_log, metric_log, asynchronous_metric_log and text_log
- profile: max_threads 2, max_block_size 8192, parallel parsing/formatting set to 0

ALTERNATIVES:
1. Langfuse Cloud Hobby (free): 50k units/month, 30 days of data access, 2 users, unlimited projects, about 30 req/min API limit, no credit card. It uses 0 MB of local RAM and is the identical product and API, so moving to self-hosted (or Railway) later is a config change. The cost is that prompts and completions leave the machine; EU and US regions exist.
2. Arize Phoenix (arizephoenix/phoenix, v20.19.0 on 2026-10-01): a single container with UI on 6006 and OTLP gRPC on 4317. It uses SQLite by default, or Postgres via `PHOENIX_SQL_DATABASE_URL` plus `PHOENIX_SQL_DATABASE_SCHEMA=phoenix`, so it can live in our shared pgvector DB. It is free with no feature gates and has `PHOENIX_DEFAULT_RETENTION_POLICY_DAYS`. Footprint is probably 300-600 MB (unverified). It is the best fully-local lightweight option and is OTel-native, but its prompt management and LiteLLM-native integration are weaker than Langfuse's.
3. Helicone: self-hosting still needs ClickHouse. The single-node recommendation is 8 vCPU / 32 GB / 250 GB. The site banner says "Helicone Joins Mintlify", so its future direction is unclear. Not recommended.
4. LiteLLM's own spend logs: rows go to LiteLLM's Postgres tables, with a UI Logs page. Settings: `store_prompts_in_spend_logs: true`, `maximum_spend_logs_retention_period: "7d"`. Zero extra services. It covers cost, tokens, latency and errors per key/model, but has no agent trace trees, evals or prompt management.

RECOMMENDATION for Phase 1:
- (a) Always enable LiteLLM spend logs with prompts stored and 30d retention in shared Postgres. This is the local baseline.
- (b) Send traces to Langfuse Cloud Hobby via the `langfuse_otel` callback and Hermes' OTel/Langfuse hooks, with keys in .env.
- (c) Define a self-hosted Langfuse v4 block in compose under `profiles: ["observability"]`, sharing Postgres (db `langfuse`) and Redis (`/2` plus `REDIS_KEY_PREFIX`). It is off by default and is enabled only when Ollama is stopped, or on Railway later.
- If data must stay local, use Phoenix-on-Postgres instead of Cloud.

## CMDS
# --- secrets (.env) ---
NEXTAUTH_SECRET=$(openssl rand -base64 32)
SALT=$(openssl rand -base64 32)
ENCRYPTION_KEY=$(openssl rand -hex 32)
CLICKHOUSE_PASSWORD=$(openssl rand -hex 16)
MINIO_ROOT_PASSWORD=$(openssl rand -hex 16)
LANGFUSE_INIT_PROJECT_PUBLIC_KEY=pk-lf-$(openssl rand -hex 16)
LANGFUSE_INIT_PROJECT_SECRET_KEY=sk-lf-$(openssl rand -hex 24)

# --- shared postgres init (initdb.d/10-langfuse.sql) ---
CREATE ROLE langfuse LOGIN PASSWORD '<pw>';
CREATE DATABASE langfuse OWNER langfuse;

# --- shared redis must run: redis-server --requirepass $REDIS_PASSWORD --maxmemory-policy noeviction

# --- compose fragment (profile, off by default) ---
x-lf-env: &lf-env
  NEXTAUTH_URL: http://localhost:3000
  DATABASE_URL: postgresql://langfuse:${LANGFUSE_DB_PASSWORD}@postgres:5432/langfuse
  SALT: ${SALT}
  ENCRYPTION_KEY: ${ENCRYPTION_KEY}
  TELEMETRY_ENABLED: "false"
  CLICKHOUSE_URL: http://clickhouse:8123
  CLICKHOUSE_MIGRATION_URL: clickhouse://clickhouse:9000
  CLICKHOUSE_USER: clickhouse
  CLICKHOUSE_PASSWORD: ${CLICKHOUSE_PASSWORD}
  CLICKHOUSE_CLUSTER_ENABLED: "false"
  CLICKHOUSE_MAX_OPEN_CONNECTIONS: "5"
  REDIS_CONNECTION_STRING: redis://:${REDIS_PASSWORD}@redis:6379/2
  REDIS_KEY_PREFIX: "langfuse:"
  LANGFUSE_INGESTION_QUEUE_PROCESSING_CONCURRENCY: "4"
  LANGFUSE_TRACE_UPSERT_WORKER_CONCURRENCY: "4"
  LANGFUSE_S3_EVENT_UPLOAD_BUCKET: langfuse
  LANGFUSE_S3_EVENT_UPLOAD_REGION: auto
  LANGFUSE_S3_EVENT_UPLOAD_ACCESS_KEY_ID: minio
  LANGFUSE_S3_EVENT_UPLOAD_SECRET_ACCESS_KEY: ${MINIO_ROOT_PASSWORD}
  LANGFUSE_S3_EVENT_UPLOAD_ENDPOINT: http://minio:9000
  LANGFUSE_S3_EVENT_UPLOAD_FORCE_PATH_STYLE: "true"
  LANGFUSE_S3_EVENT_UPLOAD_PREFIX: events/
  LANGFUSE_S3_MEDIA_UPLOAD_BUCKET: langfuse
  LANGFUSE_S3_MEDIA_UPLOAD_REGION: auto
  LANGFUSE_S3_MEDIA_UPLOAD_ACCESS_KEY_ID: minio
  LANGFUSE_S3_MEDIA_UPLOAD_SECRET_ACCESS_KEY: ${MINIO_ROOT_PASSWORD}
  LANGFUSE_S3_MEDIA_UPLOAD_ENDPOINT: http://minio:9000
  LANGFUSE_S3_MEDIA_UPLOAD_FORCE_PATH_STYLE: "true"
  LANGFUSE_S3_MEDIA_UPLOAD_PREFIX: media/
services:
  langfuse-worker:
    image: docker.langfuse.com/langfuse/langfuse-worker:4   # pin e.g. :4.54.0 in practice
    profiles: ["observability"]
    environment: { <<: *lf-env, NODE_OPTIONS: "--max-old-space-size=512" }
    mem_limit: 768m
    depends_on: [postgres, redis, clickhouse, minio]
  langfuse-web:
    image: docker.langfuse.com/langfuse/langfuse:4
    profiles: ["observability"]
    ports: ["127.0.0.1:3000:3000"]
    mem_limit: 1g
    environment:
      <<: *lf-env
      NODE_OPTIONS: "--max-old-space-size=768"
      NEXTAUTH_SECRET: ${NEXTAUTH_SECRET}
      LANGFUSE_S3_MEDIA_UPLOAD_ENDPOINT: http://localhost:9090
      LANGFUSE_S3_MEDIA_UPLOAD_INTERNAL_ENDPOINT: http://minio:9000
      AUTH_DISABLE_SIGNUP: "true"
      LANGFUSE_INIT_ORG_ID: aios
      LANGFUSE_INIT_ORG_NAME: AIOS
      LANGFUSE_INIT_PROJECT_ID: aios-dev
      LANGFUSE_INIT_PROJECT_NAME: aios-dev
      LANGFUSE_INIT_PROJECT_RETENTION: 30
      LANGFUSE_INIT_PROJECT_PUBLIC_KEY: ${LANGFUSE_INIT_PROJECT_PUBLIC_KEY}
      LANGFUSE_INIT_PROJECT_SECRET_KEY: ${LANGFUSE_INIT_PROJECT_SECRET_KEY}
      LANGFUSE_INIT_USER_EMAIL: ${LANGFUSE_ADMIN_EMAIL}
      LANGFUSE_INIT_USER_NAME: admin
      LANGFUSE_INIT_USER_PASSWORD: ${LANGFUSE_ADMIN_PASSWORD}
  clickhouse:
    image: clickhouse/clickhouse-server:25.12   # >=25.12 required by v4; 26.4 recommended
    profiles: ["observability"]
    user: "101:101"
    mem_limit: 1536m
    environment: { CLICKHOUSE_DB: default, CLICKHOUSE_USER: clickhouse, CLICKHOUSE_PASSWORD: "${CLICKHOUSE_PASSWORD}", TZ: UTC }
    volumes:
      - lf_ch_data:/var/lib/clickhouse
      - ./clickhouse/low-mem.xml:/etc/clickhouse-server/config.d/low-mem.xml:ro
      - ./clickhouse/low-mem-users.xml:/etc/clickhouse-server/users.d/low-mem-users.xml:ro
    healthcheck: { test: "wget --no-verbose --tries=1 --spider http://localhost:8123/ping || exit 1", interval: 5s, retries: 10 }
  minio:
    image: cgr.dev/chainguard/minio
    profiles: ["observability"]
    entrypoint: sh
    command: -c 'mkdir -p /data/langfuse && minio server --address ":9000" --console-address ":9001" /data'
    environment: { MINIO_ROOT_USER: minio, MINIO_ROOT_PASSWORD: "${MINIO_ROOT_PASSWORD}" }
    ports: ["127.0.0.1:9090:9000"]
    mem_limit: 256m
    volumes: [lf_minio:/data]

# --- clickhouse/low-mem.xml (Altinity KB + ClickHouse tips) ---
<clickhouse>
  <mysql_port remove="1"/><postgresql_port remove="1"/>
  <query_thread_log remove="1"/><opentelemetry_span_log remove="1"/><processors_profile_log remove="1"/>
  <trace_log remove="1"/><metric_log remove="1"/><asynchronous_metric_log remove="1"/><text_log remove="1"/>
  <mlock_executable>false</mlock_executable>
  <mark_cache_size>268435456</mark_cache_size>
  <index_mark_cache_size>67108864</index_mark_cache_size>
  <uncompressed_cache_size>16777216</uncompressed_cache_size>
  <max_concurrent_queries>8</max_concurrent_queries>
  <max_server_memory_usage_to_ram_ratio>0.75</max_server_memory_usage_to_ram_ratio>
  <background_pool_size>2</background_pool_size>
  <background_merges_mutations_concurrency_ratio>2</background_merges_mutations_concurrency_ratio>
  <background_common_pool_size>2</background_common_pool_size>
  <background_schedule_pool_size>8</background_schedule_pool_size>
  <background_move_pool_size>1</background_move_pool_size>
  <background_fetches_pool_size>1</background_fetches_pool_size>
  <merge_tree>
    <merge_max_block_size>1024</merge_max_block_size>
    <max_bytes_to_merge_at_max_space_in_pool>1073741824</max_bytes_to_merge_at_max_space_in_pool>
    <number_of_free_entries_in_pool_to_lower_max_size_of_merge>2</number_of_free_entries_in_pool_to_lower_max_size_of_merge>
    <number_of_free_entries_in_pool_to_execute_mutation>2</number_of_free_entries_in_pool_to_execute_mutation>
    <number_of_free_entries_in_pool_to_execute_optimize_entire_partition>2</number_of_free_entries_in_pool_to_execute_optimize_entire_partition>
  </merge_tree>
</clickhouse>
# --- clickhouse/low-mem-users.xml ---
<clickhouse><profiles><default>
  <max_threads>2</max_threads><max_block_size>8192</max_block_size>
  <input_format_parallel_parsing>0</input_format_parallel_parsing>
  <output_format_parallel_formatting>0</output_format_parallel_formatting>
  <max_bytes_before_external_group_by>536870912</max_bytes_before_external_group_by>
  <max_bytes_before_external_sort>536870912</max_bytes_before_external_sort>
</default></profiles></clickhouse>

# --- LiteLLM (config.yaml) -> Langfuse Cloud now, self-host later by swapping host ---
litellm_settings:
  callbacks: ["langfuse_otel"]
general_settings:
  store_prompts_in_spend_logs: true
  maximum_spend_logs_retention_period: "30d"
  maximum_spend_logs_retention_interval: "1d"
# env: LANGFUSE_PUBLIC_KEY=pk-lf-..., LANGFUSE_SECRET_KEY=sk-lf-..., LANGFUSE_OTEL_HOST=https://cloud.langfuse.com  (later: http://langfuse-web:3000)

# start/stop optional self-hosted stack
docker compose --profile observability up -d
docker compose --profile observability stop langfuse-web langfuse-worker clickhouse minio

## RISKS
- Per-container RAM figures for a tuned setup are my estimates, not measured or documented by Langfuse; Langfuse documents only production minimums (about 25 GiB total) and gives no support guidance below them.
- ClickHouse capped at 1.5 GB may hit 'Memory limit (total) exceeded' during Langfuse v4 migrations, background merges or full-text-search indexing; whether v4's lightweight updates and text indexes fit within these low-memory settings is untested.
- The low-memory ClickHouse XML comes from the Altinity KB and ClickHouse tips, not from Langfuse. Removing system log tables and shrinking pools on 25.12/26.x was not validated against Langfuse's migrations.
- Sharing Redis forces maxmemory-policy noeviction on the whole instance, so other app uses of that Redis can't rely on LRU eviction.
- LANGFUSE_INIT_PROJECT_RETENTION: schema shows int >= 3; whether data retention enforcement is available in the OSS (non-EE) self-hosted build was not verified.
- MinIO: the official compose uses cgr.dev/chainguard/minio; upstream MinIO community image/maintenance status was not re-verified; Langfuse docs also list SeaweedFS as an option. For Railway later, an S3-compatible bucket can replace MinIO.
- Langfuse Cloud Hobby sends prompt/completion content off-machine (privacy tradeoff); API limit of about 30 req/min applies to the public API, and the ingestion limits for the 50k units/month were not checked in detail.
- Phoenix's RAM footprint is not documented; the 300-600 MB figure is unverified. PHOENIX_ENABLE_AUTH/PHOENIX_SECRET weren't confirmed on the config page I fetched.
- Helicone: the 'Helicone Joins Mintlify' banner suggests a change of ownership or direction; its details and the project's future weren't verified. The GitHub repo is still active (pushed 2026-09-16, not archived).
- The Hermes Agent's native Langfuse/OTel export capability was not researched here.
- The official compose publishes host ports 5432/6379/8123/9000/9090; when merging into our stack, drop the duplicate postgres/redis services and avoid host-port collisions.
- Langfuse ships near-daily releases (v4.47 to v4.54 in about a week); pin an exact tag (e.g. 4.54.0) rather than the floating :4.

## SOURCES
https://langfuse.com/self-hosting/deployment/docker-compose
https://raw.githubusercontent.com/langfuse/langfuse/main/docker-compose.yml
https://github.com/langfuse/langfuse/releases/tag/v4.0.0
https://api.github.com/repos/langfuse/langfuse/releases (v4.54.0, 2026-10-07)
https://langfuse.com/self-hosting/upgrade/upgrade-guides/upgrade-v3-to-v4
https://langfuse.com/self-hosting/configuration/scaling
https://langfuse.com/self-hosting/deployment/infrastructure/clickhouse
https://langfuse.com/self-hosting/deployment/infrastructure/cache
https://langfuse.com/self-hosting/deployment/infrastructure/postgres
https://langfuse.com/self-hosting/deployment/infrastructure/containers
https://langfuse.com/self-hosting/configuration
https://langfuse.com/self-hosting/administration/headless-initialization
https://raw.githubusercontent.com/langfuse/langfuse/main/packages/shared/src/env.ts
https://raw.githubusercontent.com/langfuse/langfuse/main/worker/src/env.ts
https://raw.githubusercontent.com/langfuse/langfuse/main/web/src/env.mjs
https://langfuse.com/pricing
https://clickhouse.com/docs/operations/tips#using-less-than-16gb-of-ram
https://kb.altinity.com/altinity-kb-setup-and-maintenance/configure_clickhouse_for_low_mem_envs/
https://www.ssdnodes.com/learn/lang/uk/self-host-langfuse-agent-tracing (third-party low-RAM OOM report)
https://arize.com/docs/phoenix/self-hosting
https://arize.com/docs/phoenix/self-hosting/configuration
https://github.com/Arize-ai/phoenix/releases (v20.19.0, 2026-10-01)
https://docs.helicone.ai/getting-started/self-host/cloud
https://www.helicone.ai/blog/self-hosting-journey
https://docs.litellm.ai/docs/observability/langfuse_integration
https://docs.litellm.ai/docs/proxy/ui_logs