# syntax=docker/dockerfile:1
# Nightly pg_dump -> bucket for Railway (cron service). pg_dump must be >= the server major (17).
# Build context: repository root.
FROM postgres:17.11-alpine3.24@sha256:b0f9560a2de083e2cc7382e75f808c7381a32852a7ec49117deedb300e552b24
RUN apk add --no-cache rclone
COPY --chmod=0755 infra/railway/db-backup/backup.sh /usr/local/bin/aios-db-backup
USER postgres
ENTRYPOINT ["/usr/local/bin/aios-db-backup"]
