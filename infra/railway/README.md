# Railway control plane (Phase 7: ARCHITECTURE §20, §22.5, §24.17, §25)

This directory prepares the move of the control plane to Railway. **Nothing here deploys anything or calls the Railway API.** You run the cutover by hand: [docs/RUNBOOK.md](../../docs/RUNBOOK.md) has the procedure, under "Migração para Railway". That section also covers rollback.

```
Railway (control plane)                                         tailnet                  ThinkPad (local worker)
 hermes ── litellm ── decision ── knowledge ── sandbox            │
   │          │           │            │                          │
 postgres (PG17+pgvector)  redis   aios-storage / aios-backups    │
 db-backup (cron)                                                 │
 edge-gw (tag:aios-cloud) ── :8093 / :11434 ─────────────────────►│ tag:thinkpad: edge :8093, Ollama :11434 (tailscale serve)
          ◄── :4000 :8090 :8092 :8642 (ACL-gated) ────────────────│ edge worker, kb CLI, your devices
```

## Config-as-Code vs Infrastructure-as-Code

Railway deprecated per-service `railway.json` (Config as Code), as checked in the docs on 2026-10-07:
- New services cannot opt in. Railway's changelog gives 2026-08-28 for new projects.
- Existing files stop being read on **2026-12-01**.
- The replacement is `.railway/railway.ts` (Infrastructure as Code), which the CLI evaluates (`railway config plan|apply`).

How this kit handles it:
- **Source of truth:** `<service>/railway.json` holds build and deploy settings. These files are Railway's own schema, validated against a vendored copy of `https://railway.com/railway.schema.json`. `<service>/variables.env` holds the variables and `services.json` the topology.
- **`make railway-iac`:** prints the equivalent `.railway/railway.ts`. The GitHub repo comes from the `origin` remote; override it with `repo=<owner>/<name>`.
  - It is generated from the same files and contains no secrets.
  - It was type-checked and evaluated against the `railway` SDK 3.13.0 (`railway/iac`): run the tests with `RAILWAY_SDK_DIR=<dir with node_modules/{railway,typescript}>`.
  - Save it at the repository root as `.railway/railway.ts`, then run `railway config plan` (read-only) and review the plan before `railway config apply`.
- **A project created before the CaC cutoff:** each service's "Railway Config File" setting can point to `/infra/railway/<service>/railway.json`. That path is absolute, because it does not follow the Root Directory. This works only until 2026-12-01.

## Layout

| Path | What |
|---|---|
| `services.json` | services, the Redis template and buckets: names, root directories, ports, volumes |
| `shared.env` | names of the Railway **shared variables** (secrets have no value in git) |
| `<service>/railway.json` | `build.builder=DOCKERFILE`, `dockerfilePath`, `watchPatterns`; `deploy.healthcheckPath/Timeout`, `restartPolicyType`, `numReplicas`, `requiredMountPath`, `cronSchedule`, `startCommand` |
| `<service>/variables.env` | the service's variables, ready for its Raw Editor; references only |
| `<service>/Dockerfile` + scripts | thin images where compose relies on bind mounts or host networking |
| `lib/maintenance.sh` | `AIOS_MAINTENANCE=1` hold (health check only, no application), sourced by the litellm and hermes start scripts |
| `hermes/hermes_state.py` | export/import of a Hermes state directory with consistent SQLite copies (cutover, rollback) |
| `postgres/initdb/00-local-socket.sh` | sourced before `01-init.sh`: keeps the first-boot init on the Unix socket |
| `variables.md` | every `.env` variable → Railway, the reference syntax, what changes, the ThinkPad after the cutover |
| `lib/manifest.py` | loads and validates all of the above; prints the plan and the IaC |
| `tests/` | `run.sh` (hermetic), shims, schema validator, compose-parity test |

## Services

| Service | Build (root → Dockerfile) | Port (private) | Volume | Health check | Restart |
|---|---|---|---|---|---|
| `postgres` | `/` → `infra/railway/postgres/Dockerfile` (`pgvector/pgvector:0.8.7-pg17-trixie` + repo initdb + compose tuning) | 5432 | `pgdata` `/var/lib/postgresql/data` (required) | — (TCP) | ALWAYS |
| `redis` | Railway Redis template | 6379 | template | template | template |
| `litellm` | `/` → `infra/railway/litellm/Dockerfile` (pinned image + template, rendered at boot) | 4000 | — | `/health/liveliness` (300 s) | ON_FAILURE ×10 |
| `decision` | `/` → `infra/railway/decision/Dockerfile` (decision build + `config/routing.yaml`, `config/decision/rules.yaml`, `agents/` baked in) | 8080 | — | `/healthz` | ON_FAILURE ×10 |
| `knowledge` | `/workers/kb` → `Dockerfile` | 8080 | — | `/healthz` | ON_FAILURE ×10 |
| `sandbox` | `/workers/sandbox` → `Dockerfile`; `startCommand` writes the keys from variables | 22 | `sandbox-workspace` `/workspace` (required) | — (TCP) | ALWAYS |
| `hermes` | `/` → `infra/railway/hermes/Dockerfile` (pinned image + plugin, config, agents, workflows, skills) | 8642 | `hermes-data` `/opt/data` (required) | `/health` (300 s) | ALWAYS |
| `edge-gw` | `/` → `infra/railway/edge-gw/Dockerfile` (tailscale 1.102.5 binaries + socat on Alpine) | 8093, 11434 | `edge-gw-state` `/var/lib/tailscale` | — | ALWAYS |
| `db-backup` | `/` → `infra/railway/db-backup/Dockerfile` (`postgres:17.11-alpine` + rclone) | — | — | — | cron `0 6 * * *` UTC, ON_FAILURE ×2 |
| `aios-storage`, `aios-backups` | Railway Buckets (`iad`) | — | — | — | — |

Every service has one replica, because state lives in volumes. Nothing gets a public domain. Remote access goes through the tailnet (edge-gw) or `railway ssh`.

### Design notes

- **postgres** uses the same image as local, not the pgvector template (PG18):
  - dumps restore in both directions without a major-version jump, which keeps rollback trivial;
  - the stock Railway Postgres has no pgvector.
  - `infra/docker/postgres/initdb` creates the roles, databases and extensions on the first boot.
  - The service never sets `PGHOST`/`PGHOSTADDR`:
    - the executable init scripts inherit its environment, and a bare `psql` would dial TCP while the first-boot server listens on the socket only;
    - the failed init would never be retried.
  - Defences: `make railway-check` rejects those variables, and the image sources `00-local-socket.sh` (installed 0644) first. `tests/integration.sh` proves it against a real socket-only server.
- **litellm** writes `NAME=1` markers for the provider keys present in the environment, then reuses `infra/scripts/render-litellm-config.py`. The rendered config keeps `os.environ/…` references, so no secret is written to disk. Adding a key on Railway is just a redeploy.
  - With `AIOS_MAINTENANCE=1` (shared, `1` until the cutover copy), it only answers `/health/liveliness`. LiteLLM's own startup would run its Prisma migrations and write the master-key hash into the cloud `litellm` database, which `railway-db-migrate.sh` must find empty.
- **decision** duplicates the two build stages of `decision/Dockerfile`. The repository-root context is what lets it bake in the policy files compose bind-mounts. A test fails if the Go or base images drift.
- **hermes** keeps the image's s6 `ENTRYPOINT`. `main-wrapper.sh` runs an executable `CMD` as the `hermes` user after the stage2 bootstrap; this was checked in the v2026.9.24 source. `start.sh` then:
  1. with `AIOS_MAINTENANCE=1`, only answers `/health`. The container and its volume stay reachable through `railway ssh` and `railway volume files`, and the gateway never starts;
  2. installs the sandbox key from `AIOS_SANDBOX_SSH_KEY_B64`;
  3. imports `/opt/data/hermes-import.tgz` if present, before anything opens `state.db`:
     - the archive comes from `hermes_state.py export` (RUNBOOK §9.3 step 6);
     - the volume's previous content moves to `.pre-import-*`, so no foreign `-wal`/`-shm` pairs with the imported databases;
     - a corrupt archive stops the boot without touching the volume;
  4. re-applies `infra/hermes/setup.py` (idempotent: config, profiles, plugin link, cron). A success writes `/opt/data/.aios-setup-ok`;
  5. execs `hermes gateway run`.

  A failed `setup.py` is tolerated only when that marker exists. The image's stage2 hook seeds Hermes' stock `config.yaml` before `start.sh` runs, so "a config exists" would also let a never-configured gateway start: no aios plugin guards, no sandbox terminal, no LiteLLM routing.

  `rehost.py` maps the compose hostnames in the image's copy of `config/hermes/*.yaml` to `*.railway.internal`.
- **sandbox**: Railway has neither file mounts nor tmpfs. `startCommand` writes `authorized_keys` and a stable host key from variables, then execs the image's normal `tini → aios-sandbox-entrypoint`. It behaves the same whether Railway's start command replaces the ENTRYPOINT or the CMD. Without the `/run/aios` tmpfs, `GITHUB_TOKEN` is refused by the entrypoint, by design.
  - `startCommand` fails at once without `AIOS_SANDBOX_HOST_KEY_B64` or `AIOS_SANDBOX_SSH_PUBKEY`. A host key generated on the container's own disk would change on every redeploy, and Hermes' `known_hosts` (`accept-new`, on its volume) would then reject the sandbox.
  - It also writes `AIOS_EDGE_URL` (edge-gw `:8093`) to `/etc/profile.d/aios-railway.sh`, because the media skill's `edge_pipeline.sh` runs in sandbox SSH sessions and would otherwise default to `http://edge:8080`. Hermes snapshots its terminal environment from a login shell, so that file reaches the skill.
- **edge-gw** runs `tailscaled --tun=userspace-networking` (Railway has no `/dev/net/tun`) as `tag:aios-cloud`:
  - `EDGE_GW_FORWARD` listens on the private network and dials through `tailscale nc`;
  - `EDGE_GW_EXPOSE` relays tailnet ports to Railway services with `tailscale serve --tcp` → `127.0.0.1` → socat;
  - Funnel is never used. The state on its volume keeps a stable node name and 100.x address.
- **db-backup** streams `pg_dump -Fc` of `BACKUP_DATABASES` into the bucket, verifies the uploaded size and prunes after `BACKUP_RETENTION_DAYS`. A failed upload fails the cron run and deletes the partial object. Turn on Railway volume backups for `pgdata` and `hermes-data` too: Hermes' `state.db` lives there.
  - Railway Buckets use virtual-hosted-style URLs, but rclone's `Other` provider defaults to path-style. Hence `RCLONE_CONFIG_AIOS_FORCE_PATH_STYLE=false`.

### Integration with other components (done)

- **sandbox**: `write_session_env` passes `AIOS_EDGE_URL` (validated as `http(s)://host[:port]`) to every SSH
  session; the `/etc/profile.d` line in `infra/railway/sandbox/railway.json` stays as a fallback for login shells.
  The edge bearer is the Kanban worker's tenant key (`EDGE_TENANT_KEYS`, set per worker by the aios plugin), forwarded
  with `SendEnv` and admitted by `AcceptEnv`.
- **backup.sh**: `hermes-data.tgz` is `hermes_state.py export data/hermes`, consistent copies of every SQLite
  database whether Hermes runs or not.

## Scripts and make targets

| Make target | Script | Writes? |
|---|---|---|
| `railway-plan` | `infra/scripts/railway-plan.sh` | never: offline view of services, variables and the `.env` status of shared secrets (values hidden), plus drift |
| `railway-check` | `railway-plan.sh --check` | never |
| `railway-iac repo=o/n` | `railway-plan.sh --iac` | never: prints `.railway/railway.ts` |
| `db-migrate-dry` | `infra/scripts/railway-db-migrate.sh` | dry run by default. `--apply` (+ typed host or `--yes`) does dump → restore (single transaction, owner roles) → exact row-count check → pending migrations → ANALYZE. It refuses: a non-empty target (a migrated cloud `litellm` gets the recovery steps); a target without pgvector; an older major; a remote target without sslmode; running local writers; other sessions on the target databases (listed by the dry run) |
| `storage-sync-dry` | `infra/scripts/storage-sync.sh` | `rclone copy --dry-run` by default. `--apply` copies and verifies. `--mirror` (sync) can delete; `--pull` is for rollback |
| `tailscale-edge-dry` | `infra/scripts/tailscale-edge.sh` | dry run by default. `--apply` runs `tailscale serve --tcp 8093/11434` (tailnet only) and `--off` removes it. It refuses while Funnel is on. It prints the ACL (grants for `tag:aios-cloud` → `tag:thinkpad:8093,11434`) |
| `restore-drill` | `infra/scripts/railway-restore-drill.sh` | only the scratch DB `aios_restore_test`: it restores the newest `backups/<ts>/aios.dump`, compares row counts with the live DB, checks pgvector and the other artifacts, then drops it |
| `railway-test` | `infra/railway/tests/run.sh` | never |

`railway-db-migrate.sh` was also run end to end against a scratch PostgreSQL 17 + pgvector cluster (`tests/integration.sh`). It handled role creation, TOC filtering, restore as owner roles, verification and a pending migration (`013_bench`). It refuses to run again on the populated target.

## Tests

```bash
bash infra/railway/tests/run.sh                 # or: make railway-test
```

The tests use no Docker, network or Railway. `docker`, `psql`, `pg_dump`, `pg_restore`, `rclone`, `tailscale`, `tailscaled`, `socat` and `curl` are PATH shims that log every call. The cases assert:
- every script is dry by default and never writes without `--apply`;
- no secret reaches argv;
- the `railway.json` files validate against Railway's schema;
- the manifest rejects literal secrets and dangling references;
- compose and `variables.env` are in parity, and the image pins match;
- the service entrypoints behave:
  - litellm render and maintenance hold;
  - hermes start: setup marker, stock-config refusal, maintenance hold, state import;
  - edge-gw plan, db-backup, sandbox `startCommand` (fail-fast keys, `AIOS_EDGE_URL`);
- the Railway postgres image's initdb scripts never see `PGHOST`, through an emulation of the docker-library entrypoint with a control case;
- the Hermes state round trip. Rows committed only in a `-wal` survive the export. Another instance's `-wal`/`-shm` is moved aside on import. Corrupt or traversing archives change nothing;
- rclone gets `FORCE_PATH_STYLE=false` (virtual-hosted) for Railway Buckets and path style for R2;
- `mk/railway.mk` parses and never applies.

Two optional extras run when their variables are set:
- `RAILWAY_SDK_DIR=<dir with node_modules/{railway,typescript}>` type-checks the generated IaC.
- `RAILWAY_IT_PGBIN=<PG17+ bin dir with pgvector>` together with `RAILWAY_IT_SOURCE_URL=<aios DSN, read only>` runs `tests/integration.sh`:
  - it starts a throwaway cluster, does a real copy, checks owners, login and pgvector, and confirms that a populated target is refused;
  - it then boots the image's initdb scripts against a socket-only server with a TCP `PGHOST` set. Without the guard that fails; with it, the roles, databases and extensions exist.

## ENV

New variables this component introduces. The values live on Railway, or in your shell for the ThinkPad scripts, never in `.env.example`.

| Variable | Secret | Default | Where |
|---|---|---|---|
| `TS_AUTHKEY` | yes | — | Railway shared → edge-gw (first login only) |
| `TS_STATE` | no | `mem:` in the script, `/var/lib/tailscale/tailscaled.state` on Railway | edge-gw |
| `TS_HOSTNAME` | no | `aios-railway` | edge-gw |
| `TS_TAGS` | no | `tag:aios-cloud` | edge-gw |
| `TS_SOCKET` | no | `/tmp/tailscaled.sock` | edge-gw |
| `EDGE_GW_FORWARD` | no | — (Railway: `8093=thinkpad:8093,11434=thinkpad:11434`) | edge-gw |
| `EDGE_GW_EXPOSE` | no | empty (Railway: 4000/8090/8092/8642 relays) | edge-gw |
| `EDGE_GW_DRY_RUN` | no | `0` | edge-gw (tests) |
| `AIOS_SANDBOX_SSH_KEY_B64` | yes | — | Railway shared → hermes |
| `AIOS_SANDBOX_SSH_PUBKEY` | no | — | Railway shared → sandbox |
| `AIOS_SANDBOX_HOST_KEY_B64` | yes | — | Railway shared → sandbox |
| `AIOS_SETUP_ARGS` | no | empty | hermes (extra `setup.py` flags at boot) |
| `AIOS_MAINTENANCE` | no | shared `1` (cutover: `0`) | Railway shared → litellm, hermes (`1` = health check only, application not started) |
| `AIOS_EDGE_URL` | no | `http://${{edge-gw.RAILWAY_PRIVATE_DOMAIN}}:8093` | sandbox (exported into the agent's login profile) |
| `AIOS_RAILWAY_LIB` | no | `/opt/aios/railway` | litellm/hermes start scripts: directory of `maintenance.sh` and `hermes_state.py` (tests) |
| `AIOS_PYTHON`, `AIOS_SETUP`, `HERMES_BIN`, `HERMES_HOME` | no | `/opt/hermes/.venv/bin/python`, `/opt/aios/bin/setup.py`, `hermes`, `/opt/data` | hermes `start.sh` (tests) |
| `BACKUP_DATABASES` | no | `aios,litellm` | db-backup |
| `BACKUP_RETENTION_DAYS` | no | `14` | db-backup |
| `BACKUP_REMOTE` | no | — (Railway: `aios:${{aios-backups.BUCKET}}/postgres`) | db-backup |
| `RCLONE_CONFIG_AIOS_*` | key: yes | — | db-backup (Railway), `storage-sync.sh` (shell) |
| `TARGET_DATABASE_URL` | yes | — | shell → `railway-db-migrate.sh` (superuser URL, through the tunnel) |
| `PG_CLIENT_IMAGE` | no | `pgvector/pgvector:0.8.7-pg17-trixie` | shell → `railway-db-migrate.sh` |
| `MIGRATE_WORK_DIR` | no | `backups` | shell → `railway-db-migrate.sh` |
| `STORAGE_SYNC_REMOTE` | no | `aios` | shell → `storage-sync.sh` |
| `STORAGE_S3_FORCE_PATH_STYLE` | no | `false` (`true` for an R2 endpoint) | shell → `storage-sync.sh` (`true` for a path-style bucket) |
| `EDGE_TAILNET_PORTS` | no | `8093,11434` | shell → `tailscale-edge.sh` |
| `RESTORE_DB` | no | `aios_restore_test` (must end in `_restore_test`) | shell → `railway-restore-drill.sh` |
| `KEEP` | no | `0` | shell → `railway-restore-drill.sh` (keep the scratch DB) |
| `AIOS_ROOT`, `LITELLM_ENTRYPOINT` | no | `/opt/aios`, `/app/docker/prod_entrypoint.sh` | litellm entrypoint (tests) |
| `AIOS_RAILWAY_DIR`, `RAILWAY_SDK_DIR`, `RAILWAY_IT_PGBIN`, `RAILWAY_IT_SOURCE_URL` | no | — | tests only |

Railway-level variables (`PORT`, `PGDATA`, `RAILWAY_SHM_SIZE_BYTES`, `STORAGE_S3_*` from the bucket) are listed in [variables.md](variables.md).
