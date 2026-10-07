# Service contracts (V1)

These are the binding interfaces between components. `docs/ARCHITECTURE.md` is the vision; this file is the implementation contract.

## 0. Responsibility split (decided 2026-10-07)

| Component | Owns | Does **not** own |
|---|---|---|
| **Hermes** (runtime) | agents (profiles), sessions, skills, cron, hooks, tools, execution, delegation, Kanban tasks/handoffs, webhooks, messaging | memory of record, billing, policy |
| **Decision Service** (Go, the only Go) | Jev integration and typed decisions; **routing policy** (which tier/model, which agent); **budget** (ledger, limits, cost per task); **escalation** (gate done/repair/escalate/fail) | execution, task scheduling, agent loop |
| **knowledge** (Python) | knowledge base, layered memory, Context Compiler, `storage://`, MCP tools | agent runtime |
| **edge** (Python) | local media: ffmpeg, Whisper, local summarization (§22) | agent runtime |
| **sandbox** (container) | isolated execution for Hermes' `terminal` (SSH backend) | decisions |
| **LiteLLM** | gateway: providers, fallbacks, per-service virtual keys | policy |

**No component other than Hermes runs an agent loop.** Hermes consults the Decision Service at the **gates** through its plugin `aios`.

## 1. Topology

| Service | Dir | Container port → host (127.0.0.1) | Profile |
|---|---|---|---|
| postgres | — | 5432 → 5432 | core |
| valkey | — | 6379 internal | core |
| litellm | config/litellm | 4000 → 4000 | core |
| decision | decision/ | 8080 → 8090 | core |
| knowledge | workers/kb | 8080 → 8092 | core |
| hermes | config/hermes, tools/hermes-plugin | 8642 → 8642 | agent |
| sandbox | workers/sandbox | 22 internal | agent |
| edge | workers/edge | 8080 → 8093 | media |
| whisper | — | 8080 → 8178 | media |
| langfuse-* | — | 3000 → 3000 | observability |
| Ollama | host | 11434 | — |

- Every HTTP service has `GET /healthz` (process) and `GET /readyz` (dependencies), both without auth.
- Everything else requires `Authorization: Bearer <SERVICE>_API_KEY`.
- Logs are JSON to stdout.
- Configuration comes only from env vars, with the same names on Railway.

Migrations: `001–005` core · `006–009` decision (policy/ledger) · `010–011` knowledge · `012` edge · `013` bench.

## 2. Decision Service (`http://decision:8080`)

### 2.1 Typed decisions (done)

`POST /v1/decide` runs the rules → local → Jev → OpenAI cascade. It returns typed answers with confidence and `needs_human`.

### 2.2 Routing policy

`POST /v1/route` takes a task and decides tier, model, agent and needs (§6, §13):

```json
{"session_id":"…","task_id":"…","text":"…","agent":"chief?","tenant":"pessoal?","domain":"?","task_type":"?","complexity":"?"}
→ {"run_id":"uuid","domain":"engineering","agent":"engineering","task_type":"debugging","complexity":"medium",
   "tier":3,"model":"tier3-code","needs_research":false,"needs_confirmation":false,
   "budget":{"max_cost_usd":0.5,"max_iterations":8,"token_budget":{...}},"skills":["coding/debugging"],
   "decision_trace":[...]}
```

- Fields the caller already sends are not asked again.
- The rest go to the cascade.
- The tier comes from `config/routing.yaml`: `task_types[*].tier_by_complexity`, then domain overrides, then learned stats (once `min_samples` is reached), capped by the agent's `max_tier`.
- It creates or updates the **run** in the ledger, keyed by `session_id`. Calling it again for the same session returns the current run's state; it does not reset it.

`GET /v1/runs/{session_id}` returns the run state: tier, model, cost, tokens, iterations, failures, status.

`POST /v1/models/resolve` takes `{session_id, requested_model}` and returns `{model}`. It is the per-LLM-call path for the Hermes middleware, and it is cheap: no LLM involved.
- It returns the run's current tier model, escalated if applicable.
- With no run, it returns `default_model` from routing.yaml.
- Requests for `tier7-*` without an approved approval are lowered to tier 6.

### 2.3 Budget

`POST /v1/usage` records the usage of one LLM call:

```json
{"session_id","task_id?","model","input_tokens","output_tokens","cache_read_tokens","api_request_id","purpose?","success":true}
→ {"cost_usd","run":{…totals},"budget":{"state":"ok|warn|exhausted","reason"}}
```

- Cost is computed from the prices in `config/routing.yaml` (`models.*.price`). It is written to `llm_calls` and aggregated into the run.
- This call is idempotent on `api_request_id`.

`POST /v1/budget/check` takes `{session_id}` and returns `{state: ok|warn|exhausted, reasons[], remaining:{usd,tokens,iterations}}`. It checks these limits:
- run: `max_cost_usd`, `token_budget.total`, `max_iterations`, `deadline`;
- agent: `max_cost_per_run`;
- global: `budgets` table (day/month).

### 2.4 Escalation (gate)

`POST /v1/gate` is the decision gate (§7, §2 DONE/REPAIR/ESCALATE):

```json
{"session_id","evidence":{"validation":"pass|fail|none","failures":["…"],"tests":{"exit_code":1,"tail":"…"},
 "confidence?":0.4,"architectural_change?":false,"critical?":false}}
→ {"action":"done|repair|escalate|fail|ask_human","next_model":"tier4-pro","tier":4,"message":"…","reasons":[…]}
```

Deterministic guards are applied first:
- an exhausted budget gives `fail`;
- `escalate_after_failures` (default 2) consecutive failures at the same tier give `escalate`;
- a next tier above `max_tier` gives `ask_human` (with an `approvals` row);
- a validation `pass` gives `done`.

Otherwise the Decision Service asks the cascade/Jev `next_step` (choice) using the evidence as state. An escalation updates the run's tier, and the next `models/resolve` call already returns the new model.

`POST /v1/runs/{session_id}/finish` records the outcome `{status: succeeded|failed|cancelled, task_type?}` and closes the run, which feeds the cost-per-successful-task stats.

`POST /v1/approvals/{id}` takes `{approve, by}`. `GET /v1/approvals?status=pending` lists approvals.

### 2.5 Reports (§18)

`GET /v1/reports/{costs|models|agents|loops|routes|escalations}` returns cost per successful task by `task_type × model`, the most expensive agent, the model that solves the most tasks, loops (repairs per run), escalation rate, and cost/success by route.

## 3. Hermes (runtime)

- **Profiles (agents §4):** `chief` (default home: gateway, API server, cron, Kanban dispatcher), plus `engineering`, `finance`, `projects`, `personal` and `learning`.
  - Each profile has its own `SOUL.md`, `config.yaml` (toolsets, skills dirs) and memory.
  - Only the **chief gateway** stays resident. Domain profiles run on demand as **Kanban workers**: `kanban_create(assignee=<domain>, tenant=<tenant>)`, spawned by the dispatcher.
  - The **tenant** is the Kanban `tenant`; workers receive `HERMES_TENANT`.
- **Plugin `aios`** (tools/hermes-plugin/aios, enabled in every profile):
  - `register_system_prompt_section`: static AIOS policy, a stable prefix for the cache (§16).
  - `llm_request` middleware: `model = decision /v1/models/resolve(session)` (routing + escalation).
  - `post_api_request` / `api_request_error`: `decision /v1/usage`. A `warn`/`exhausted` budget notifies, and `exhausted` blocks new tools.
  - `pre_llm_call` (first turn of the session): `decision /v1/route`, plus `knowledge /v1/context/compile`, which is injected as context (§9).
  - `pre_tool_call`: permissions from `agents/<profile>/agent.yaml` (allowed/deny tools and domains, deny > allow), a secret-file guard (`.env`, `*.pem`, `id_*`), a destructive-command guard, and a tenant guard on knowledge MCP tools (the `tenant` argument must match `HERMES_TENANT`/the session).
  - `pre_verify` (code edited): run `aios-task-check` in the sandbox, then `decision /v1/gate`. `repair` → `{"action":"continue","message":diagnosis}`. `escalate` → continue on the new model. `done` → finish. `fail`/`ask_human` → finish with a report.
  - `on_session_end`: `decision /v1/runs/{id}/finish`.
- **MCP:** `mcp_servers.knowledge.url = http://knowledge:8080/mcp/` with header `Authorization: Bearer ${KNOWLEDGE_API_KEY}`.
- **Terminal:** `terminal.backend: ssh` → `sandbox:22`, user `agent`.
- **Cron (§17):**
  - `morning-review` 07:30, `daily-planning` 08:00, `engineering-review` 09:00, `project-review` 18:30, `learning-review` 20:00 and `daily-reflection` 22:30, plus `weekly-review` on Sundays at 19:00.
  - Prompts live in `workflows/*.md` and are created by `make hermes-cron`. Webhooks use Hermes webhook routes (`messaging/webhooks`).
- **Skills (§5):** `skills/<category>/<name>/SKILL.md`, mounted read-only and pointed to by `skills.external_dirs`.

## 4. Knowledge (`http://knowledge:8080`)

See `workers/kb/README.md`.

- `POST /v1/search`, `/v1/context/compile`, `/v1/memories`, `/v1/memories/search`, `/v1/ingest`, `/v1/maintain`, `GET /v1/stats`.
- `/mcp` serves the tools `knowledge_search`, `compile_context`, `memory_save`, `memory_search` and `ingest_note`.
- `KNOWLEDGE_TENANTS` is the instance's ceiling. Per-session isolation is enforced by the `aios` `pre_tool_call` hook.

## 5. Edge (`http://edge:8080`, §22 agent-edge)

| Endpoint | Behavior |
|---|---|
| `POST /extract_audio` | `{source: storage://…}` → `storage://media/processing/<id>.wav` (16 kHz mono) |
| `POST /transcribe` | `{source, language='pt', diarize=false}` → `{text, segments[{start,end,text,speaker?}], txt_uri, srt_uri}` (whisper-server) |
| `POST /summarize` | `{text \| transcript_uri, kind: summary\|notes\|tasks\|topics\|all, model='local-qwen'}` → JSON (map-reduce) |
| `POST /process_video` | audio → transcript → summary |
| `POST /embed` | `{texts[]}` → vectors |
| `POST /pipeline` | `{source, tenant, domain?, ingest:true}`: the full chain, then `knowledge /v1/ingest` |
| `POST /upload` | multipart → `storage://media/input/<id>.<ext>` |

- Retention: `media/archive` keeps files for `MEDIA_RETENTION_DAYS` (30), and `processing` is cleared at the end of each job.
- Diarization is an optional extra (pyannote). Without it, the call returns 501.

## 6. Sandbox (`sandbox:22`, §21)

The sandbox is a container with sshd, user `agent`, toolchains (git, Go, Node + pnpm, Python + uv, ruff, eslint, tsc, duckdb) and `/workspace`. It has no docker.sock, no host mounts beyond `data/sandbox/*`, and runs with `cap_drop: ALL` plus the minimum sshd needs.

Helpers:
- `aios-task-start <id> <repo> [ref]`: clones into `/workspace/tasks/<id>` on branch `aios/<id>`.
- `aios-task-check <id> [cmd…]`: runs tests or lint. Returns JSON `{exit_code, output_tail, command}`.
- `aios-task-patch <id>`: writes `/workspace/patches/<id>.patch`.
- `aios-task-destroy <id>`.

## 7. Bench (`bench/`, §24.18)

- 60 tasks: 10 simple, 10 medium, 10 debugging, 5 refactor, 5 agentic, 5 research, 5 finance and 5 media.
- Each is submitted to **Hermes** (`POST /v1/runs` with `session_id=bench-<task>-<n>`) and checked deterministically or by an LLM judge.
- Cost, tokens, iterations and tier come from `decision /v1/runs/{session_id}`. Results go to `bench_runs`.
- `bench report` gives success and cost per success by category × model, plus a suggested `routing.yaml` diff.

## 8. LiteLLM virtual keys

Each service gets its own key, with a model allowlist and a budget:

| Key | Models |
|---|---|
| `HERMES_LITELLM_KEY` | tiers 2–6 |
| `DECISION_LITELLM_KEY` | local + tier2-cheap |
| `KB_LITELLM_KEY` | embed + local + tier 2 |
| `EDGE_LITELLM_KEY` | local-qwen, embed-local, tier 2 |
| `BENCH_LITELLM_KEY` | judge, tier 5 |

Tier 7 needs an approval.
