# Railway variables

How every `.env` variable moves to Railway, what the cloud adds, and what stays on the ThinkPad.
The machine-readable source is `infra/railway/<service>/variables.env` plus `infra/railway/shared.env`.
`make railway-plan` prints both and shows which shared secrets your `.env` already has, without printing any values.
`make railway-check` validates every reference.

## How Railway variables work

These facts were checked against docs.railway.com on 2026-10-07.

| Mechanism | Syntax | Used for |
|---|---|---|
| Shared variable (Project Settings → Shared Variables) | `${{shared.NAME}}` | every secret and `TZ`, defined once and referenced by each service that needs it |
| Another service's variable | `${{postgres.RAILWAY_PRIVATE_DOMAIN}}`, `${{redis.REDISPASSWORD}}` | hostnames and credentials Railway generates |
| Bucket credentials | `${{aios-storage.BUCKET}}`, `.ENDPOINT`, `.ACCESS_KEY_ID`, `.SECRET_ACCESS_KEY`, `.REGION` | `STORAGE_S3_*`, rclone |
| The service's own variable | `${{RAILWAY_PRIVATE_DOMAIN}}` | the postgres `DATABASE_URL` |
| Composition | `postgresql://aios:${{shared.AIOS_DB_PASSWORD}}@${{postgres.RAILWAY_PRIVATE_DOMAIN}}:5432/aios` | DSNs: the password stays a reference |

- **Private networking.** Each service is `<service>.railway.internal` inside the project environment, over WireGuard.
  - Use `http://`, not `https://`.
  - Environments created after 2025-10-16 resolve to both IPv4 and IPv6. Legacy environments are IPv6-only, so there `API_SERVER_HOST` and uvicorn's `--host` would need `::`.
  - The private network does not exist during the build, so migrations and seeding run at start: `hermes/start.sh` and the postgres initdb.
- **`PORT`.** Railway health-checks the port named by `PORT`, so each HTTP service sets `PORT` to its listen port explicitly. Health checks run only during a deploy. They are not monitoring.
- **Volumes.**
  - Volumes are mounted at start, not during the build or the pre-deploy step. A service keeps one volume.
  - Images that run as non-root need `RAILWAY_RUN_UID=0`. None of ours do: Hermes starts as root and drops to `hermes` itself.
- **Buckets.** Buckets are S3-compatible and reachable only over the public endpoint. They use virtual-hosted-style URLs: `ENDPOINT` is the base URL and the client adds the bucket.
  - Buckets created before that change may need path-style requests; the bucket's Credentials tab says which.
  - rclone's `Other` provider defaults to path-style, so every rclone remote here sets `FORCE_PATH_STYLE=false`: db-backup's `RCLONE_CONFIG_AIOS_FORCE_PATH_STYLE` and `storage-sync.sh`'s default.
- **Paste or CLI.** Each `variables.env` is in the format of the service's **Raw Editor** (Variables tab), so you can paste it.
  - For one value from the CLI without echoing it: `railway variable set NAME --stdin -s <service>`.
  - Shared variables are pasted in Project Settings → Shared Variables.
- **No secret value is committed.** `make railway-check` fails on a literal secret, on a DSN with a literal password, and on a reference to an unknown service, bucket or shared variable.

## What changes (compose → Railway)

| Local (compose) | Railway | Variables that carry it |
|---|---|---|
| `postgres:5432` | `postgres.railway.internal:5432`. The service is built from `infra/railway/postgres` with the same PG17 + pgvector image | `DATABASE_URL` (decision, litellm), `KB_DATABASE_URL`, `PGHOST` (db-backup) |
| `valkey:6379` | Railway Redis template `redis`, which generates its own password | `REDIS_ADDR=${{redis.REDISHOST}}:${{redis.REDISPORT}}`, `REDIS_HOST`/`REDIS_PORT`/`REDIS_PASSWORD` |
| `litellm:4000` | `litellm.railway.internal:4000` | `LITELLM_BASE_URL`; the Hermes config is rehosted at image build |
| `decision:8080` | `decision.railway.internal:8080` | `AIOS_DECISION_URL` (Hermes) |
| `knowledge:8080` | `knowledge.railway.internal:8080` | `AIOS_KNOWLEDGE_URL`; the MCP URL in the Hermes config is rehosted |
| `sandbox:22` | `sandbox.railway.internal:22` | `TERMINAL_SSH_HOST`; `terminal.ssh_host` is rehosted |
| `host.docker.internal:11434` (Ollama) | `edge-gw.railway.internal:11434` → tailnet → ThinkPad | `OLLAMA_API_BASE` (litellm), `OLLAMA_BASE_URL` (decision) |
| `edge:8080` / host `:8093` | `edge-gw.railway.internal:8093` → tailnet → ThinkPad | sandbox `AIOS_EDGE_URL` (the sandbox `startCommand` exports it into the agent's login profile, which Hermes' terminal snapshot reads). the bearer is the worker's tenant key from `EDGE_TENANT_KEYS` (the aios plugin sets `EDGE_API_KEY` per Kanban worker; Hermes forwards it with `SendEnv`, the sandbox accepts it with `AcceptEnv`) |
| `./data/storage` | bucket `aios-storage` (`infra/scripts/storage-sync.sh`) | `STORAGE_S3_*` |
| bind mounts of `config/`, `agents/`, `skills/`, `workflows/`, plugin | baked into the images (`decision`, `hermes`) | none; a push rebuilds the image (watch patterns) |
| `./data/hermes` | volume `hermes-data` at `/opt/data` | none |
| `make backup` (03:00) | cron service `db-backup` at `0 6 * * *` UTC, writing to bucket `aios-backups`; plus Railway volume backups | `BACKUP_*`, `RCLONE_CONFIG_AIOS_*` |
| Langfuse (`make obs-up`) | Langfuse Cloud | `LANGFUSE_HOST`, `LANGFUSE_PUBLIC_KEY`/`SECRET_KEY` |

The Hermes config templates (`config/hermes/*.yaml`) contain compose hostnames. `infra/railway/hermes/rehost.py` rewrites the image's own copy to `<service>.railway.internal` at build time; the repository files stay compose-shaped. A test makes sure no compose hostname is left in the rewritten copy.

## Every `.env` variable

**Shared** means a Railway shared variable that services reference with `${{shared.NAME}}`.

### Locale and host

| `.env` | Secret | Railway | Notes |
|---|---|---|---|
| `TZ` | no | shared `TZ` (default `America/Sao_Paulo`) | decision `BUDGET_TZ`, hermes, sandbox. postgres stays `UTC`, as in compose |
| `RENDER_GID` | no | — | GPU group for whisper; ThinkPad only |
| `PG_HOST_PORT` | no | — | no host port in the cloud; use `railway connect postgres --tunnel-only`, or the `ssh -L` tunnel in RUNBOOK §9.3 |

### Postgres

| `.env` | Secret | Railway | Notes |
|---|---|---|---|
| `PG_SUPERUSER_PASSWORD` | **yes** | shared | postgres `POSTGRES_PASSWORD`/`PGPASSWORD`, db-backup, migration target |
| `AIOS_DB_PASSWORD` | **yes** | shared | postgres initdb, decision `DATABASE_URL`, knowledge `KB_DATABASE_URL` |
| `AIOS_READER_PASSWORD` | **yes** | shared | postgres initdb |
| `LITELLM_DB_PASSWORD` | **yes** | shared | postgres initdb, litellm `DATABASE_URL` |
| `LANGFUSE_DB_PASSWORD` | **yes** | shared | postgres initdb creates the role even when Langfuse Cloud is used |

### Redis

| `.env` | Secret | Railway | Notes |
|---|---|---|---|
| `REDIS_PASSWORD` | yes | — | replaced by the template's own `${{redis.REDISPASSWORD}}` |

### LiteLLM

| `.env` | Secret | Railway | Notes |
|---|---|---|---|
| `LITELLM_MASTER_KEY` | **yes** | shared → litellm only | admin key; never on Hermes or decision |
| `LITELLM_SALT_KEY` | **yes** | shared → litellm | **must equal the local value**: it decrypts what the migrated `litellm` DB stores |
| `HERMES_LITELLM_KEY` | **yes** | shared → hermes `LITELLM_API_KEY` | virtual keys live in the `litellm` DB, so they keep working after the migration |
| `DECISION_LITELLM_KEY` | **yes** | shared → decision `LITELLM_API_KEY` | |
| `KB_LITELLM_KEY` | **yes** | shared → knowledge | |
| `EDGE_LITELLM_KEY` | yes | — | the edge worker stays on the ThinkPad (its `.env`) and calls the cloud LiteLLM over the tailnet |
| `BENCH_LITELLM_KEY` | yes | — | bench runs from the ThinkPad |

### Model providers

| `.env` | Secret | Railway | Notes |
|---|---|---|---|
| `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`, `MOONSHOT_API_KEY`, `DEEPSEEK_API_KEY` | **yes** | shared → litellm | an empty one drops that provider from the config rendered at boot |
| `OPENAI_API_KEY` | **yes** | shared → litellm, decision | |
| `TYPESAFE_API_KEY` | **yes** | shared → decision | Jev; add `jev` to `DECISION_BACKENDS` |
| `JEV_MODEL` | no | decision literal `jev-latest` | |

### Hermes

| `.env` | Secret | Railway | Notes |
|---|---|---|---|
| `HERMES_TAG` | no | baked into `infra/railway/hermes/Dockerfile` (tag + digest) | a test fails if it differs from `.env.example` or compose |
| `HERMES_API_KEY` | **yes** | shared → hermes `API_SERVER_KEY` | the API is full terminal access: no public domain |
| `HERMES_API_KEY_<PROFILE>` | **yes** | shared → hermes | `setup.py` writes each into `profiles/<p>/.env` as `API_SERVER_KEY` (`/p/<p>/` auth) |
| `HERMES_WEBHOOK_SECRET` | **yes** | shared → hermes, decision | |
| `AIOS_DEFAULT_TENANT` | no | hermes literal (empty) | |
| `AIOS_CRON_DELIVER` | no | hermes literal `local` | in the cloud nobody reads local output: switch to `telegram` once `TELEGRAM_*` is set |

### Decision Service

| `.env` | Secret | Railway | Notes |
|---|---|---|---|
| `DECISION_API_KEY` | **yes** | shared → decision, hermes | |
| `DECISION_BACKENDS` | no | decision literal `rules,local` | `local` needs the ThinkPad awake (Ollama through edge-gw); prefer `rules,jev` if it sleeps |
| `DECISION_THRESHOLD`, `DECISION_LOCAL_MODE`, `OLLAMA_DECIDER_MODEL`, `OPENAI_DECISIONS_MODEL` | no | decision literals (`.env.example` defaults) | `make railway-plan` reports drift from your `.env` |

### Knowledge

| `.env` | Secret | Railway | Notes |
|---|---|---|---|
| `KNOWLEDGE_API_KEY` | **yes** | shared → knowledge, hermes | |
| `KNOWLEDGE_TENANTS` | no | knowledge literal `nitro,pessoal,shared` | |
| `KB_EMBED_MODEL`, `KB_EMBED_DIM` | no | — | host `kb` CLI only; the service uses the same code defaults |

### Notifications

| `.env` | Secret | Railway | Notes |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | **yes** | shared → hermes, decision | |
| `TELEGRAM_ALLOWED_USERS` | yes | shared → hermes | Hermes allowlist |
| `TELEGRAM_CHAT_ID` | yes | shared → decision | |
| `NTFY_URL` | yes | shared → decision | the topic URL is the capability |

### Edge worker

| `.env` | Secret | Railway | Notes |
|---|---|---|---|
| `EDGE_API_KEY` | **yes** | — | admin bearer: only the ThinkPad edge worker and the host `make edge-*` use it |
| `EDGE_TENANT_KEYS` | **yes** | shared → hermes | `tenant:key,...`; the edge on the ThinkPad must have the same value |

### Langfuse

| `.env` | Secret | Railway | Notes |
|---|---|---|---|
| `LANGFUSE_HOST` | no | shared, default `https://cloud.langfuse.com` | litellm `LANGFUSE_OTEL_HOST`, hermes `HERMES_LANGFUSE_BASE_URL` |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | **yes** | shared → litellm, hermes | Langfuse Cloud project keys |
| `LANGFUSE_TAG`, `NEXTAUTH_SECRET`, `LANGFUSE_SALT`, `LANGFUSE_ENCRYPTION_KEY`, `CLICKHOUSE_PASSWORD`, `MINIO_ROOT_PASSWORD`, `LANGFUSE_INIT_*`, `LANGFUSE_ADMIN_*` | yes / no | — | self-hosted Langfuse stays local (`make obs-up`). A Railway Langfuse template brings its own variables |

Component variables that are not in `.env.example` yet are mapped in the `variables.env` files:
- sandbox: `AIOS_CHECK_TIMEOUT`, `AIOS_TASK_CHECK_TIMEOUT` and `AIOS_CLONE_TIMEOUT` are set on Railway. `GITHUB_TOKEN` is not: it needs the `/run/aios` tmpfs, Railway has none, and the entrypoint refuses to write the token to disk.
- skills: `AIOS_EDGE_URL` (default `http://edge:8080` in `edge_pipeline.sh`) is set on the sandbox. A parity test fails when a skill script defaults an `AIOS_*_URL` to a compose host that the sandbox does not remap.
- edge: `WHISPER_URL`, `EDGE_*` and `MEDIA_RETENTION_DAYS` stay on the ThinkPad.

## Railway-only variables (new)

| Variable | Service | Secret | Default | Purpose |
|---|---|---|---|---|
| `PORT` | litellm 4000, decision 8080, knowledge 8080, hermes 8642 | no | — | health-check port |
| `PGDATA` | postgres | no | `/var/lib/postgresql/data/pgdata` | data lives in a subdirectory, because the volume root holds `lost+found` |
| `RAILWAY_SHM_SIZE_BYTES` | postgres | no | `268435456` | 256 MB `/dev/shm`, like compose `shm_size` (HNSW builds) |
| `PGUSER`, `PGPASSWORD`, `PGDATABASE`, `PGPORT` | postgres | password: yes (reference) | — | libpq defaults for `railway ssh -s postgres -- psql` (local socket) |
| `DATABASE_URL` | postgres | password: yes (reference) | — | read by `railway connect`, never by libpq |
| — `PGHOST`, `PGHOSTADDR` | postgres | — | **never set** | the image's executable initdb scripts inherit the service environment, so a TCP host would abort the first boot (the init server listens on the socket only). `make railway-check` rejects it, and `infra/railway/postgres/initdb/00-local-socket.sh` unsets both |
| `AIOS_MAINTENANCE` | litellm, hermes (`${{shared.AIOS_MAINTENANCE}}`) | no | shared `1` | `1`: the container starts and answers its health check, but LiteLLM (Prisma migrations, master-key hash) and the Hermes gateway do not run, while `railway ssh`/`volume files` keep working. Set it to `1` before creating the services and to `0` at RUNBOOK §9.3 step 7. Rollback sets it back to `1` |
| `TS_AUTHKEY` | edge-gw | **yes** | — | tagged (`tag:aios-cloud`), pre-approved auth key, needed for the first login only |
| `TS_STATE` | edge-gw | no | `/var/lib/tailscale/tailscaled.state` | node state on the `edge-gw-state` volume: stable name and 100.x address. `mem:` makes it ephemeral |
| `TS_HOSTNAME`, `TS_TAGS` | edge-gw | no | `aios-railway`, `tag:aios-cloud` | tailnet identity |
| `EDGE_GW_FORWARD` | edge-gw | no | `8093=thinkpad:8093,11434=thinkpad:11434` | Railway private network → ThinkPad. Use the MagicDNS name that `tailscale-edge.sh` prints |
| `EDGE_GW_EXPOSE` | edge-gw | no | litellm 4000, decision 8090, knowledge 8092, hermes 8642 | tailnet → Railway (gated by the ACL; bearer keys still required) |
| `AIOS_SANDBOX_SSH_KEY_B64` | hermes | **yes** | — | `base64 -w0 data/sandbox/keys/id_ed25519`. `start.sh` writes it to `/opt/aios/ssh/id_ed25519` with mode 0600 |
| `AIOS_SANDBOX_SSH_PUBKEY` | sandbox | no | — | contents of `data/sandbox/keys/id_ed25519.pub` (authorized key) |
| `AIOS_SANDBOX_HOST_KEY_B64` | sandbox | **yes** | — | a dedicated ed25519 host key, so Hermes' `known_hosts` survives redeploys. Required: the `startCommand` fails without it, and also without `AIOS_SANDBOX_SSH_PUBKEY` |
| `AIOS_EDGE_URL` | sandbox | no | `http://${{edge-gw.RAILWAY_PRIVATE_DOMAIN}}:8093` | edge worker URL for `transcript_to_notes`. The `startCommand` validates it and writes `/etc/profile.d/aios-railway.sh` |
| `AIOS_SETUP_ARGS` | hermes | no | empty | extra `setup.py` flags at boot (e.g. `--recreate-cron`) |
| `STORAGE_S3_BUCKET`, `_ENDPOINT`, `_REGION`, `_ACCESS_KEY_ID`, `_SECRET_ACCESS_KEY` | knowledge (prepared) | key: yes (reference) | bucket `aios-storage` | used once the knowledge image installs its `s3` extra (boto3); `STORAGE_BACKEND` stays `local` until then |
| `BACKUP_DATABASES`, `BACKUP_RETENTION_DAYS`, `BACKUP_REMOTE` | db-backup | no | `aios,litellm`, `14`, `aios:${{aios-backups.BUCKET}}/postgres` | nightly dumps |
| `RCLONE_CONFIG_AIOS_*` | db-backup | key: yes (reference) | bucket `aios-backups` | rclone remote defined only by variables, with `FORCE_PATH_STYLE=false` (virtual-hosted). To keep a copy off Railway, point it at R2 with `FORCE_PATH_STYLE=true` |

`infra/scripts/storage-sync.sh` on the ThinkPad reads the same `STORAGE_S3_*` names, or `RCLONE_CONFIG_AIOS_*` directly, from your shell environment. Copy the values from the bucket's Credentials tab. Do not put them in `.env`. `STORAGE_S3_FORCE_PATH_STYLE` sets the URL style:
- default `false` (Railway Buckets);
- `true` for an R2 endpoint, or for a bucket whose Credentials tab says "path".

## The ThinkPad after the cutover

The laptop keeps Ollama, whisper and the edge worker, and becomes `tag:thinkpad` on the tailnet.

`edge-gw` relays these cloud APIs onto the tailnet as `aios-railway:<port>`:

| Port | Service |
|---|---|
| 4000 | litellm |
| 8090 | decision |
| 8092 | knowledge |
| 8642 | hermes |

Get the gateway's stable address with `tailscale ip -4 aios-railway`. Docker containers usually cannot resolve MagicDNS names (Docker passes the host's upstream resolvers, not the Tailscale one), so map the name with `extra_hosts`. The `edge` service in compose.yaml hard-codes compose hostnames, so point the edge worker at the cloud with a local `compose.override.yaml`, which docker compose loads automatically:

```yaml
services:
  edge:
    extra_hosts: ["aios-railway:100.x.y.z"]   # tailscale ip -4 aios-railway
    environment:
      LITELLM_BASE_URL: http://aios-railway:4000
      KNOWLEDGE_URL: http://aios-railway:8092
```

The host `kb` CLI talks to Postgres directly. Open `railway connect postgres --tunnel-only -P 15432` (or the `ssh -L` tunnel in RUNBOOK §9.3), then:

```bash
KB_DATABASE_URL=postgresql://aios:<AIOS_DB_PASSWORD>@127.0.0.1:15432/aios \
LITELLM_BASE_URL=http://aios-railway:4000 make kb-ingest tenant=...
```
