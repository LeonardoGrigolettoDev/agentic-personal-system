# Decision Service (Tier 1 — `docs/ARCHITECTURE.md` §6)

Answers **typed questions about a state** (binary / choice / score) with calibrated probabilities.
It is used at the orchestrator gates (route, continue/repair/escalate/done), never after every tool call.

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

**Enabling Jev:** set `TYPESAFE_API_KEY` and `DECISION_BACKENDS=rules,local,jev` in `.env`, then run `docker compose up -d decision`.

## Dev

```bash
go vet ./... && go test ./...
```
