#!/usr/bin/env bash
# Create .env from .env.example and fill every empty generated secret. Idempotent: never overwrites a value.
set -euo pipefail
cd "$(dirname "$0")/../.."
[[ -f .env ]] || cp .env.example .env
chmod 600 .env

set_if_empty() {
  local k=$1 v=$2
  grep -qE "^${k}=.+" .env && return 0
  if grep -qE "^${k}=" .env; then sed -i "s|^${k}=.*|${k}=${v}|" .env; else echo "${k}=${v}" >>.env; fi
}
# append keys added to .env.example after .env was created
while IFS= read -r line; do
  [[ "$line" =~ ^([A-Z0-9_]+)= ]] || continue
  grep -qE "^${BASH_REMATCH[1]}=" .env || echo "$line" >>.env
done <.env.example

hex() { openssl rand -hex "$1"; }
b64() { openssl rand -base64 32 | tr -d '\n'; }

for k in PG_SUPERUSER_PASSWORD AIOS_DB_PASSWORD AIOS_READER_PASSWORD LITELLM_DB_PASSWORD LANGFUSE_DB_PASSWORD \
  REDIS_PASSWORD CLICKHOUSE_PASSWORD MINIO_ROOT_PASSWORD LANGFUSE_ADMIN_PASSWORD HERMES_WEBHOOK_SECRET; do
  set_if_empty "$k" "$(hex 24)"
done
set_if_empty LITELLM_MASTER_KEY "sk-$(hex 32)"
set_if_empty LITELLM_SALT_KEY "sk-$(hex 32)"
set_if_empty HERMES_API_KEY "$(hex 32)"
set_if_empty DECISION_API_KEY "$(hex 32)"
set_if_empty KNOWLEDGE_API_KEY "$(hex 32)"
set_if_empty EDGE_API_KEY "$(hex 32)"
set_if_empty EDGE_TENANT_KEYS "nitro:$(hex 24),pessoal:$(hex 24),shared:$(hex 24)"
for p in ENGINEERING FINANCE PROJECTS PERSONAL LEARNING; do
  set_if_empty "HERMES_API_KEY_${p}" "$(hex 32)"
done
set_if_empty NEXTAUTH_SECRET "$(b64)"
set_if_empty LANGFUSE_SALT "$(b64)"
set_if_empty LANGFUSE_ENCRYPTION_KEY "$(hex 32)"
set_if_empty LANGFUSE_INIT_PROJECT_PUBLIC_KEY "pk-lf-$(hex 16)"
set_if_empty LANGFUSE_INIT_PROJECT_SECRET_KEY "sk-lf-$(hex 24)"
set_if_empty LANGFUSE_ADMIN_EMAIL "$(git config user.email 2>/dev/null || echo admin@localhost)"
set_if_empty RENDER_GID "$(getent group render | cut -d: -f3 || echo 992)"

echo ".env ready. Paste the provider keys you have (ANTHROPIC/OPENROUTER/MOONSHOT/DEEPSEEK/OPENAI/TYPESAFE)."
