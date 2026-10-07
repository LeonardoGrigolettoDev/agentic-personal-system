#!/usr/bin/env bash
# Create budgeted LiteLLM virtual keys with model allowlists and write them into .env.
# Hermes and the decision service never receive the master key (it can mint keys / lift budgets).
set -euo pipefail
cd "$(dirname "$0")/../.."
set -a; source .env; set +a
URL="http://127.0.0.1:4000"

gen_key() { # alias budget_usd models_json
  local alias=$1 budget=$2 models=$3
  curl -fsS "$URL/key/generate" -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H 'Content-Type: application/json' \
    -d "{\"key_alias\":\"$alias\",\"max_budget\":$budget,\"budget_duration\":\"30d\",\"models\":$models}" | jq -r .key
}
set_env() { sed -i "s|^$1=.*|$1=$2|" .env; }

if [[ -z "${HERMES_LITELLM_KEY:-}" ]]; then
  # Tier 2-6 only; tier7-fable requires a human-approved key
  k=$(gen_key hermes 20 '["tier2-cheap","tier2-flash","tier3-code","tier4-pro","tier4-k3","tier5-sonnet","tier6-opus"]')
  set_env HERMES_LITELLM_KEY "$k" && echo "HERMES_LITELLM_KEY written"
fi
if [[ -z "${DECISION_LITELLM_KEY:-}" ]]; then
  k=$(gen_key decision 5 '["decider-local","local-qwen","tier2-cheap"]')
  set_env DECISION_LITELLM_KEY "$k" && echo "DECISION_LITELLM_KEY written"
fi
if [[ -z "${KB_LITELLM_KEY:-}" ]]; then
  k=$(gen_key kb 5 '["embed-local","local-qwen","tier2-cheap","tier2-flash"]')
  set_env KB_LITELLM_KEY "$k" && echo "KB_LITELLM_KEY written"
fi
echo "virtual keys ready"
