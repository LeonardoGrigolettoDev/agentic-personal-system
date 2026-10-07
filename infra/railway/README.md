# Railway migration (Phase 7 — docs/ARCHITECTURE.md §25)

Nothing here runs yet. Mapping for when the control plane moves:

| Compose service | Railway | Notes |
|---|---|---|
| postgres | Railway Postgres (pgvector enabled) | `pg_dump -Fc` locally → `pg_restore`; then `infra/scripts/migrate.sh` |
| valkey | Railway Redis | same `REDIS_*` vars |
| litellm | service from `ghcr.io/berriai/litellm:<tag>` | `config.yaml` baked or mounted; `DATABASE_URL` → Railway PG |
| decision | service from `decision/Dockerfile` | `LITELLM_BASE_URL=http://litellm.railway.internal:4000` |
| hermes | service from `nousresearch/hermes-agent:<tag>` | persistent volume at `/opt/data` |
| langfuse | Langfuse Cloud or template | |
| Ollama / whisper | stay on the ThinkPad | reached over Tailscale (`agent-edge` worker, §22.5) |

Same variable names as `.env.example`; set them as Railway Variables. Files referenced as `storage://`
move to a Railway Bucket / R2 without code changes.
