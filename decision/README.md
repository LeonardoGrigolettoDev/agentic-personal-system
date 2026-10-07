# Decision Service (`docs/ARCHITECTURE.md` §6, §7, §13–15)

The only Go component of the system. It owns **typed decisions (Jev)**, **routing policy**, **budget** and
**escalation**. It does not execute agents: Hermes is the runtime and consults this service at the gates
through the `aios` plugin (CONTRACTS §0, §3).

Answers **typed questions about a state** (binary / choice / score) with calibrated probabilities.
It is used at the Hermes gates (route, continue/repair/escalate/done), never after every tool call.

Backends form a **cascade**, cheapest first. Only questions that are still unresolved move on, and an answer is accepted when `confidence ≥ threshold`:

| Backend | What | Cost |
|---|---|---|
| `rules` | `expr-lang` expressions over `state` (`config/decision/rules.yaml`). A match returns confidence 1.0 | 0 |
| `local` | Qwen3 4B on Ollama. The schema constrains the answer to single-token labels, and `top_logprobs` gives the distribution (`DECISION_LOCAL_MODE=logprobs`). The alternative `vote` mode takes 3 samples through LiteLLM | 0 |
| `jev` | **Jev** (TypeSafe AI System One): `POST /v1/systemone`. `binary` maps to `noul` | $0.042/M input |
| `openai` | OpenAI Decisions (beta, `gpt-6-luna`): `POST /v1/decisions`. Preferred when there are images | $0.10/M input |

Anything still unresolved after the last backend comes back as the best answer seen, with `needs_human: true`.
Confidence is `1 − H(p)/ln(K)`: normalized entropy over the K options, where one-hot = 1 and uniform = 0.

## API

```http
POST /v1/decide            Authorization: Bearer $DECISION_API_KEY
{"state": "...|{...}", "questions": [{"name","type","instructions","criteria"|"levels"}],
 "threshold": 0.8, "backends": ["rules","local","jev"], "images": [], "run_id": "uuid", "task_id": "uuid"}
→ {"request_id","answers":[{"name","type","choice","probability","score","confidence",
   "probabilities","backend","refused","needs_human"}],"needs_human","cached","latency_ms","backend_trace"}

POST /v1/hermes-events     X-Hermes-Signature-256: sha256=<hmac(body, HERMES_WEBHOOK_SECRET)>  → events
GET  /healthz              process
GET  /readyz               Postgres + schema_migrations contains DECISION_REQUIRED_MIGRATION
```

Every request is logged to the `decisions` table: backend, probabilities, latency and cost. That table is the golden set for measuring local-vs-Jev agreement. Final answers are cached in Valkey (DB 1) for 1 hour.

Example: `make smoke`, which uses `testdata/route.json`.

## Routing, budget and escalation

Policy: `config/routing.yaml` (tiers, prices, task types × complexity, ladder) plus `agents/*/agent.yaml` (max_tier,
max_cost_per_run, token_budget, max_iterations). Ledger: `agent_runs` keyed by the Hermes `session_id` (migration 006).

| Endpoint | Purpose |
|---|---|
| `POST /v1/route` | Classifies domain, task_type, complexity, needs_research and needs_confirmation. It asks the cascade only what the caller didn't send, then chooses the agent and the starting model (policy + learned stats, capped by `max_tier`). It opens the run. |
| `POST /v1/models/resolve` | The hot path of every LLM call (Hermes `llm_request` middleware): the run's current model. `tier7` needs an approval. |
| `POST /v1/usage` | Ledger for one call. Cost is computed from `routing.yaml` prices. It is idempotent on `api_request_id`. Returns the budget state. |
| `POST /v1/budget/check` | Checks the run's cost, tokens, iterations and deadline, plus the global day/month budget (`budgets` table) → `ok\|warn\|exhausted` |
| `POST /v1/gate` | Escalation gate: deterministic guards first (budget, pass → done, N failures → escalate, `max_tier` → ask_human + approval, critical/architectural/low confidence → escalate). When ambiguous, it asks the cascade/Jev `next_step`. Returns `action`, `next_model` and a `message` for Hermes to continue with. |
| `GET /v1/runs/{session}` · `POST /v1/runs/{session}/finish` | Run state; closes the run (feeds the cost per successful task). |
| `GET /v1/approvals` · `POST /v1/approvals/{id}` | Human approvals: a tier above `max_tier`, or Fable. |
| `GET /v1/reports/{costs,models,agents,skills,loops,routes,escalations,spend}` | The §18 questions. |
| `GET /v1/policy` | The effective policy. |

**Learned routing:** for each `(task_type, start_model)` with at least `min_samples` finished runs, the start moves to the
model with the lowest cost per success among those at or above `target_success_rate`. This is the §14 example: K2.7 at 4×$0.25 against
Sonnet at 1×$0.80.

## ENV

| Var | Default | |
|---|---|---|
| `DECISION_API_KEY` | — (required) | bearer for `/v1/decide` |
| `DECISION_BACKENDS` | `rules,local` | cascade order; add `jev`, `openai` |
| `DECISION_THRESHOLD` | `0.8` | |
| `DECISION_LOCAL_MODE` | `logprobs` | `logprobs` \| `vote` |
| `DECISION_RULES_FILE` | — | YAML rules |
| `DECISION_TIMEOUT` / `DECISION_UPSTREAM_TIMEOUT` | `90s` / `30s` | |
| `DATABASE_URL`, `REDIS_ADDR`, `REDIS_PASSWORD`, `REDIS_DB` | | persistence / cache |
| `OLLAMA_BASE_URL`, `OLLAMA_DECIDER_MODEL` | host Ollama, `qwen3:4b-instruct-2507-q4_K_M` | |
| `LITELLM_BASE_URL`, `LITELLM_API_KEY` | | vote mode |
| `TYPESAFE_API_KEY`, `JEV_MODEL`, `JEV_BASE_URL` | `jev-latest` | Jev is registered only when the key is set |
| `OPENAI_API_KEY`, `OPENAI_DECISIONS_MODEL`, `OPENAI_BASE_URL` | `gpt-6-luna` | |
| `HERMES_WEBHOOK_SECRET` | | enables `/v1/hermes-events` |
| `DECISION_POLICY_FILE`, `DECISION_AGENTS_DIR` | `/etc/aios/routing.yaml`, `/etc/aios/agents` | policy |
| `BUDGET_TZ` | `TZ` / `America/Sao_Paulo` | boundaries of the daily/monthly budget |

**Enabling Jev:** set `TYPESAFE_API_KEY` and `DECISION_BACKENDS=rules,local,jev` in `.env`, then run `docker compose up -d decision`.

## Dev

```bash
go vet ./... && go test ./...
# ledger against a migrated Postgres:
DECISION_TEST_DATABASE_URL=postgresql://aios:...@127.0.0.1:5432/aios go test ./internal/ledger/
```
