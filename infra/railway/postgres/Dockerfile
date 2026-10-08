# syntax=docker/dockerfile:1
# Railway Postgres = the image local compose runs (PG 17 + pgvector 0.8.7), so dumps restore in both
# directions (cutover and rollback) without a major-version jump. Build context: repository root.
FROM pgvector/pgvector:0.8.7-pg17-trixie@sha256:7a7e9f22015b67edb4bef5c59daeebcd7e74bfa570df6ce60ae01237c8648a84
# First boot on an empty volume: roles aios/aios_reader/litellm/langfuse, their databases and the extensions
# (same script as compose). The schema itself always comes from migrations/ or a restored dump.
COPY infra/docker/postgres/initdb/ /docker-entrypoint-initdb.d/
# sourced first (0644): keeps the init scripts on the local socket even if PGHOST is ever set on the service
COPY --chmod=0644 infra/railway/postgres/initdb/00-local-socket.sh /docker-entrypoint-initdb.d/00-local-socket.sh
# Same tuning as compose.yaml; PGDATA is a subdirectory because the volume root holds lost+found.
CMD ["postgres", "-c", "listen_addresses=*", "-c", "shared_buffers=256MB", "-c", "effective_cache_size=768MB", \
     "-c", "work_mem=8MB", "-c", "maintenance_work_mem=128MB", "-c", "max_connections=100", \
     "-c", "max_parallel_maintenance_workers=2", "-c", "random_page_cost=1.1", "-c", "wal_compression=on", \
     "-c", "timezone=UTC"]
