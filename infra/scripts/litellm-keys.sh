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
set_env() { if grep -q "^$1=" .env; then sed -i "s|^$1=.*|$1=$2|" .env; else echo "$1=$2" >>.env; fi; }

if [[ -z "${HERMES_LITELLM_KEY:-}" ]]; then
  # tier7-fable is reachable only after a Decision Service approval (resolve caps it otherwise);
  # the model is always chosen by the aios middleware, never by the agent
  k=$(gen_key hermes 20 '["tier2-cheap","tier2-flash","tier3-code","tier4-pro","tier4-k3","tier5-sonnet","tier6-opus","tier7-fable"]')
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
if [[ -z "${EDGE_LITELLM_KEY:-}" ]]; then
  k=$(gen_key edge 5 '["local-qwen","embed-local","tier2-cheap","tier2-flash"]')
  set_env EDGE_LITELLM_KEY "$k" && echo "EDGE_LITELLM_KEY written"
fi
if [[ -z "${BENCH_LITELLM_KEY:-}" ]]; then
  k=$(gen_key bench 10 '["tier2-cheap","tier2-flash","tier5-sonnet"]') # LLM-judge for research tasks
  set_env BENCH_LITELLM_KEY "$k" && echo "BENCH_LITELLM_KEY written"
fi
echo "virtual keys ready"
