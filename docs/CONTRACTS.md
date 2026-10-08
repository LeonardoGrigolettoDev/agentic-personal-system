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
| bench | bench/ | — (one-shot `make bench-run`) | bench |
| langfuse-* | — | 3000 → 3000 | observability |
| Ollama | host | 11434 | — |

Networks: `aios` (172.30.0.0/24, the only subnet ufw admits to the host's Ollama :11434) carries every service except
the sandbox. `sandbox_net` (172.30.1.0/24) holds only the sandbox and its clients (hermes, edge, bench), so code run
in the sandbox reaches neither Ollama, whisper, Postgres nor LiteLLM.

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
{"session_id":"…","task_id":"…","text":"…","agent":"chief?","tenant":"pessoal?","domain":"?","task_type":"?","complexity":"?","model":"?"}
→ {"run_id":"uuid","domain":"engineering","agent":"engineering","task_type":"debugging","complexity":"medium",
   "tier":3,"model":"tier3-code","needs_research":false,"needs_confirmation":false,
   "budget":{"max_cost_usd":0.5,"max_iterations":8,"token_budget":{...}},"skills":["coding/debugging"],
   "decision_trace":[...]}
```

- Fields the caller already sends are not asked again.
- The rest go to the cascade.
- The tier comes from `config/routing.yaml`: `task_types[*].tier_by_complexity`, then domain overrides, then learned stats (once `min_samples` is reached), capped by the agent's `max_tier`.
- `model` (optional) pins the start model (`route_reason: "pinned by caller"`; the bench uses it to compare models). It must be a `routing.yaml` model at or below the agent's `max_tier` (400 otherwise); escalation then follows the ladder as usual.
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
- run: `max_cost_usd`, `token_budget.total`, `max_iterations`, `deadline`. The token budget counts **fresh** tokens (uncached input + output): an agent loop resends its whole prompt (≈18k tokens of tool schemas) every call, those come back as cache reads, and the cost limit already prices them;
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

`POST /v1/runs/{session_id}/finish` records the outcome `{status: succeeded|failed|cancelled, task_type?}` and closes the run, which feeds the cost-per-successful-task stats. The **first close wins** (a bench verdict is not rewritten by Hermes' later session finalize), except that `failed` evidence always sticks.

`POST /v1/approvals/{id}` takes `{approve, by}`. `GET /v1/approvals?status=pending` lists approvals.

### 2.5 Reports (§18)

`GET /v1/reports/{costs|models|agents|loops|routes|escalations}` returns cost per successful task by `task_type × model`, the most expensive agent, the model that solves the most tasks, loops (repairs per run), escalation rate, and cost/success by route.

## 3. Hermes (runtime)

- **Profiles (agents §4):** `chief` (default home: gateway, API server, cron, Kanban dispatcher), plus `engineering`, `finance`, `projects`, `personal` and `learning`.
  - Each profile has its own `SOUL.md`, `config.yaml` and memory, rendered by `make hermes-setup` (`infra/hermes/setup.py`) from `config/hermes/{config,profiles}.yaml`.
  - Only the **chief gateway** stays resident; it multiplexes the profiles (always on in v2026.9.24) and serves them at `/p/<profile>/` with each profile's own `API_SERVER_KEY` (`HERMES_API_KEY_<PROFILE>`).
  - Domain profiles run on demand as **Kanban workers**: `kanban_create(assignee=<domain>, tenant=<tenant>)`, spawned by the in-gateway dispatcher (`max_in_progress: 1`). Workers receive `HERMES_TENANT`. Only the chief has the `kanban` toolset (`platform_toolsets`); workers get their worker tools from the dispatcher.
  - **Secrets under multiplexing:** Hermes resolves every profile's secrets (the chief's included) only from that profile's `.env`, never from the container env. `hermes-setup` therefore syncs `LITELLM_API_KEY` and `KNOWLEDGE_API_KEY` into every profile `.env`, plus `TELEGRAM_*` and the Langfuse keys into the chief's. Re-run `make hermes-setup` after rotating any of them.
- **Plugin `aios`** (tools/hermes-plugin/aios, enabled in every profile):
  - Profile identity: `hermes_cli.profiles.current_profile_name()` (the request's profile on a multiplexed gateway, `HERMES_PROFILE` on a worker); `default` is the chief.
  - `register_system_prompt_section`: static AIOS policy, a stable prefix for the cache (§16).
  - `llm_request` middleware: `model = decision /v1/models/resolve(session)` (routing + escalation).
  - `post_api_request` / `api_request_error`: `decision /v1/usage`. A `warn`/`exhausted` budget notifies, and `exhausted` blocks new tools except wrap-up ones.
  - `pre_llm_call` (first turn of the session): `decision /v1/route`, plus `knowledge /v1/context/compile`, which is injected as context (§9). Tenant hint, in order: `HERMES_TENANT` (worker) > `Tenant desta execução: **x**` anywhere in the message (scheduled workflows) > `AIOS_DEFAULT_TENANT` > classification. The routed text is the user instruction without expanded skill bodies.
  - `pre_tool_call`: permissions from `agents/<profile>/agent.yaml` (tool categories, tenants, deny domains; deny > allow), a secret-file guard (`.env`, `*.pem`, `id_*`), a destructive-command guard, and the tenant guard on knowledge MCP tools (the `tenant` argument must be the session's or `shared`). Calls wrapped by the tool-search bridge are checked one by one. Sessions `bench-*` cannot use `memory`, `memory_save`, `ingest_note`, `project_upsert`, `kanban_create/link/comment`, `cronjob_manage` or `send_message`.
  - `pre_verify` (code edited): `ssh agent@sandbox /usr/local/bin/aios-check --json <changed path>`, then `decision /v1/gate`. `repair`/`escalate` → `{"action":"continue","message":…}` (escalation changes the model of the next call). `done` → finish. `fail`/`ask_human` → one final message, no loop. Tests the agent ran itself (`go test`, `pytest`, `aios-task-check`, `python -m unittest`, …) are kept as fallback evidence.
  - Edge keys: with `EDGE_TENANT_KEYS`, a Kanban worker gets only its tenant's key as `EDGE_API_KEY` (forwarded to the sandbox by the `transcript_to_notes` skill); every other process gets none.
  - `on_session_finalize`: `decision /v1/runs/{id}/finish`.
- **MCP:** `mcp_servers.knowledge.url = http://knowledge:8080/mcp/` with header `Authorization: Bearer ${KNOWLEDGE_API_KEY}`; `tools.tool_search` off so the tenant guard sees real tool names.
- **Terminal:** `terminal.backend: ssh` → `sandbox:22`, user `agent` (`personal` keeps `local` but has no shell permission).
- **Cron (§17):** run by the chief gateway, created by `make hermes-setup` from `config/hermes/profiles.yaml` with prompts in `workflows/*.md`. One tenant per job (the plugin isolates sessions): `morning-review` 07:30 (pessoal), `daily-planning` 08:00 weekdays (nitro), `engineering-review` 09:00 weekdays (nitro), `project-review` 18:30 weekdays (nitro), `learning-review` 20:00 (pessoal), `daily-reflection` 22:30 (pessoal), `weekly-review` Sun 19:00 (pessoal) and `weekly-review-nitro` Fri 17:30. No skills are attached (each workflow loads its skill with `skill_view`), so the tenant tag stays at the top of the prompt.
- **Skills (§5):** `skills/<category>/<name>/SKILL.md`, mounted read-only and pointed to by `skills.external_dirs`.

## 4. Knowledge (`http://knowledge:8080`)

See `workers/kb/README.md`.

- `POST /v1/search`, `/v1/context/compile`, `/v1/memories`, `/v1/memories/search`, `/v1/ingest`, `/v1/maintain`, `GET /v1/stats`, `GET /v1/memories`, `POST /v1/memories/{id}/deprecate`.
- `GET /v1/projects?tenant=&status=`, `POST /v1/projects {tenant, slug, name, domain?, repository?, description?, status?}`: projects that `scope="project"` memories and `compile_context(project=…)` point at. A slug belongs to one tenant (409 for another); `repository` is a git remote or `storage://`, never a host path; omitted fields are kept on update. CLI: `make kb-project` / `make kb-projects`.
- `/mcp` serves the tools `knowledge_search`, `compile_context`, `memory_save`, `memory_search`, `ingest_note`, `project_list` and `project_upsert`.
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
| `GET /jobs`, `GET /jobs/{id}` | in-memory job queue; any heavy call with `"async": true` returns `202 {job_id, status_url}` |
| `POST /maintain` | retention sweep (also runs at startup and every `EDGE_MAINTAIN_INTERVAL_HOURS`) |

- Keys: `EDGE_API_KEY` is the admin key (host `make edge-*` only). `EDGE_TENANT_KEYS` (`tenant:key,…`) are what agent sessions get: a tenant key may only `upload`, `pipeline` into its own tenant (sources in `media/input/`) and read its own `jobs`; anything else is 403/404.
- Retention: `media/archive` keeps files for `MEDIA_RETENTION_DAYS` (30), and `processing` is cleared at the end of each job.
- Diarization is an optional extra (pyannote). Without it, the call returns 501.

## 6. Sandbox (`sandbox:22`, §21)

The sandbox is a container with sshd, user `agent`, toolchains (git, Go, Node + pnpm, Python + uv, ruff, eslint, tsc, duckdb) and `/workspace`. It has no docker.sock, no host mounts beyond `data/sandbox/*`, no published port, sits only on `sandbox_net`, and runs with `cap_drop: ALL` plus the minimum sshd needs. sshd accepts only `GITHUB_TOKEN`, `EDGE_API_KEY` and `HERMES_TENANT` from clients (`AcceptEnv`).

Helpers:
- `aios-check [--json] <path>`: the plugin's validator for the project containing `path` (detects Makefile/go/npm/pnpm/uv/pytest/Poetry); one JSON line `{exit_code, output_tail, command, skipped?}`.
- `aios-task-start <id> <repo> [ref]`: clones into `/workspace/tasks/<id>` on branch `aios/<id>`.
- `aios-task-check <id> [cmd…]`: runs tests or lint. Returns JSON `{exit_code, output_tail, command}`.
- `aios-task-patch <id>`: writes `/workspace/patches/<id>.patch`.
- `aios-task-destroy <id>`.

## 7. Bench (`bench/`, §24.18)

- 55 tasks: 10 simple, 10 medium, 10 debugging, 5 refactor, 5 agentic, 5 research, 5 finance and 5 media (the per-category counts of ARCHITECTURE §24.18; the "60" there does not add up).
- Each is pre-routed (`decision /v1/route` with the task's tenant, agent and optionally a pinned `model`), submitted to **Hermes** (`POST [/p/<profile>]/v1/runs` with `session_id=bench-<task>-<n>-<ts>`) and checked deterministically or by an LLM judge. Hermes infrastructure failures are recorded as errors (`cancelled`), not model failures.
- Cost, tokens, iterations and tier come from `decision /v1/runs/{session_id}`. Results go to `bench_runs`.
- `bench report` gives success and cost per success by category × model, plus a suggested `routing.yaml` diff.

## 8. LiteLLM virtual keys

Each service gets its own key, with a model allowlist and a budget:

| Key | Models |
|---|---|
| `HERMES_LITELLM_KEY` | tiers 2–7 (tier 7 only after a Decision Service approval: `resolve` caps it otherwise) |
| `DECISION_LITELLM_KEY` | local + tier2-cheap |
| `KB_LITELLM_KEY` | embed + local + tier 2 |
| `EDGE_LITELLM_KEY` | local-qwen, embed-local, tier 2 |
| `BENCH_LITELLM_KEY` | judge, tier 5 |

