# Service contracts (V1)

Binding interfaces between components, so they can be built in parallel. `docs/ARCHITECTURE.md`
is the vision; this file is the implementation contract. Change it only together with every consumer.

## 1. Topology

| Service | Lang | Dir | Container port → host (127.0.0.1) | Profile | Auth |
|---|---|---|---|---|---|
| postgres | — | — | 5432 → 5432 | core | roles `aios` (owner), `aios_reader` (SELECT knowledge) |
| valkey | — | — | 6379 (internal) | core | `REDIS_PASSWORD` |
| litellm | — | config/litellm | 4000 → 4000 | core | virtual keys per service |
| decision | Go | decision/ | 8080 → 8090 | core | `Bearer DECISION_API_KEY` |
| orchestrator | Go | orchestrator/ | 8080 → 8091 | core | `Bearer ORCHESTRATOR_API_KEY` |
| knowledge | Python | workers/kb | 8080 → 8092 | core | `Bearer KNOWLEDGE_API_KEY` |
| sandbox | — | workers/sandbox | 22 (internal only) | agent | SSH key in `data/sandbox/keys` |
| hermes | — | config/hermes | 8642 → 8642 | agent | `Bearer HERMES_API_KEY` |
| edge | Python | workers/edge | 8080 → 8093 | media | `Bearer EDGE_API_KEY` |
| whisper | — | — | 8080 → 8178 | media | internal |
| langfuse-* | — | — | 3000 → 3000 | observability | — |
| Ollama | host | — | 11434 (host) | — | ufw: only 172.30.0.0/24 |

Inside the compose network services call each other by name (`http://orchestrator:8080`). Every
service reads config from env vars only (same names on Railway later). Every HTTP service exposes
`GET /healthz` (process) and `GET /readyz` (dependencies), unauthenticated, and logs JSON to stdout.

**Integration ownership.** Builders do **not** edit `compose.yaml`, the root `Makefile`, `.env.example`,
`infra/scripts/gen-env.sh` or `infra/scripts/litellm-keys.sh`. Each builder writes:
- `infra/compose/<service>.snippet.yaml` – the service block(s) to merge into compose.yaml;
- `mk/<component>.mk` – extra make targets (the root Makefile does `-include mk/*.mk`);
- an `ENV` section in its README listing new variables (secret? generated? default?).

## 2. Migrations

Numbered ranges (each file runs in one transaction as role `aios`, via `infra/scripts/migrate.sh`):
`001–005` core (done) · `006–009` orchestrator · `010–011` knowledge · `012` edge · `013` bench.
A migration never edits an earlier one. Grants for `aios_reader` go in the migration that creates the table.

## 3. Decision Service (`decision`, done)

`POST /v1/decide` → `{request_id, answers[], latency_ms, backend_trace}`.

Request: `{state, questions:[{name, type: binary|choice|score, instructions, criteria{}, levels[]}], threshold?, backends?, images?, run_id?, task_id?}`.
Answer: `{name, type, choice, probability, score, confidence, probabilities{}, backend, refused, needs_human}`.
Cascade order is `DECISION_BACKENDS` (rules,local[,jev][,openai]).

`POST /v1/hermes-events` takes a Hermes outbound webhook (HMAC `X-Hermes-Signature-256`) and writes it to `events`.

## 4. Orchestrator (`orchestrator`, Go) — the §2 pipeline

It owns the task lifecycle. Every trigger becomes a task (§17).

### Task API

| Method + path | Purpose |
|---|---|
| `POST /v1/tasks` | Create a task from `{title, description, tenant?, domain?, project?, task_type?, priority?, budget?, wait?:bool}`. Returns `{task_id, status}`. `wait=true` blocks until the task finishes or times out. |
| `GET /v1/tasks/{id}` | Task, runs, gate history and result. |
| `GET /v1/tasks?status=` | List tasks. |
| `POST /v1/tasks/{id}/cancel` | Cancel a task. |
| `POST /v1/approvals/{id}` | Resolve an approval with `{approve: bool, by}`. |
| `GET /v1/approvals?status=pending` | List pending approvals. |
| `POST /v1/webhooks/{source}` | HMAC-SHA256 over the raw body with `WEBHOOK_SECRET_<SOURCE>`, header `X-Signature-256: sha256=<hex>`. Writes an event, then creates a task according to `config/webhooks.yaml`. |
| `POST /v1/permissions/check` | Takes `{session_id?, agent?, resource_type, resource, action}` and returns `{effect: allow\|deny\|ask, agent, reason}`. Rules: deny > ask > allow, no match means deny. An unknown session resolves to agent `chief`. This is the endpoint the Hermes `pre_tool_call` hook calls. |
| `POST /v1/sessions/{session_id}/agent` | Register `{agent, run_id, tenant}`. The orchestrator calls this itself when it starts a run; it is exposed for tooling. |
| `GET /v1/reports/{name}` | One report per §18 question: `costs`, `models`, `skills`, `loops`, `routes`, `escalations`, `agents`. |

### Pipeline per task

Each step writes an `events` row, `type=task.<step>`.

1. **DECIDE.** Decision Service questions:
   - `domain` (choice of the 6 domains)
   - `complexity` (score: trivial / simple / medium / hard / critical)
   - `task_type` (choice from `config/routing.yaml`)
   - `needs_research` (binary)
   - `needs_confirmation` (binary)

   Rules come first, so the deterministic path costs zero tokens.
2. **CONFIRM.** If `needs_confirmation` is set, the agent policy asks, or the cost estimate exceeds the budget: create an `approvals` row, notify, and park the task as `blocked`.
3. **RETRIEVE + COMPILE.** `POST knowledge /v1/context/compile` (§5) within the `retrieval` token budget.
4. **ROUTE.** `config/routing.yaml` maps (domain, complexity, task_type) to a starting tier alias. That is then adjusted by learned stats from `v_cost_per_successful_task`, but only once a route has at least `min_samples` runs. The result is capped by the agent's `max_tier`.
5. **EXECUTE.** Run the domain agent through Hermes on session `run-<run_id>`:
   - Register the session with the orchestrator before the run.
   - Build the prompt in stable-prefix order (§16): `[static system][static agent SOUL + policy][skills hint][tools] ---- [memory][task + compiled context][tool results]`.
   - Select the model per run. The builder verifies the exact Hermes mechanism: the API `model` field, the `/model custom:litellm:<alias>` command, or a session option.
6. **VALIDATE.** Validators come from the `task_type` in routing.yaml:
   - `command`: run checks in the sandbox over SSH (tests, lint, build). Exit code plus output tail.
   - `review`: an LLM reviewer one tier above the executor returns a JSON verdict `{pass, issues[]}`.
   - `schema`: the result must be JSON matching the schema.
   - `none`
7. **GATE.**
   - Deterministic guards come first:
     - budget exhausted (tokens, cost, iterations or deadline) → `failed` + notify;
     - 2 consecutive failures at the same tier → escalate;
     - a tier above the agent's `max_tier` → approval.
   - Otherwise ask the Decision Service: `next_step` choice of `done | repair | escalate | fail`, with the validation result in its state.
   - `repair` loops back to EXECUTE with the diagnosis on the same tier. `escalate` moves one tier up the ladder in `config/routing.yaml`. `done` finalizes.
8. **FINALIZE.** Close `agent_runs`, then attribute cost: sum the LiteLLM spend logs for that run's virtual key and time window into `llm_calls` (`purpose` = step). Write the result and notify if `notify: true`, on failure, or when an approval is needed.

### Scheduler

`config/schedules.yaml` holds cron entries (5-field, TZ `America/Sao_Paulo`): `{name, cron, task:{title, description_file: workflows/<x>.md, domain, tenant}, enabled}`. Default schedule from §17:
- 07:30 morning-review
- 08:00 daily-planning
- 09:00 engineering-review
- 18:30 project-review
- 20:00 learning-review
- 22:30 daily-reflection
- weekly-review Sunday 19:00

Missed runs during sleep are caught up once (latest only). A Valkey lock prevents double runs.

### Notifications

The `Notifier` interface has these implementations:
- `telegram`: `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`.
- `ntfy`: `NTFY_URL`, optional.
- `log`: always on.

Every notification also writes an event of type `notification.sent`.

### Agents and permissions sync

On startup, and on `POST /v1/admin/sync`, the orchestrator upserts `agents/*/agent.yaml` (mounted read-only at `/etc/aios/agents`) into `agents` and `permissions`:
- `allowed_domains`, `deny_domains` and `allowed_tools` map to permission rows.
- `max_cost_per_run` maps to `agents.max_cost_per_run_usd`.

## 5. Knowledge service (`knowledge`, Python FastAPI in workers/kb, `kb serve`)

| Method + path | Purpose |
|---|---|
| `POST /v1/search` | `{tenant, query, domain?, project?, k?, include_shared?}` → `{results:[{chunk_id, document_id, title, source_uri, heading_path, score, snippet}]}` |
| `POST /v1/context/compile` | `{task, tenant, domain?, project?, budget_tokens}` → §9 Task Context `{task, project, current_state, relevant_decisions[], relevant_memories[], relevant_documents[], constraints[], token_estimate}`. Greedy fill by relevance until the budget is reached; never exceeds it. |
| `POST /v1/memories` | Create a memory: `{tenant, scope, domain?, project?, kind, lifecycle, content, importance, source_run_id?}`. Embeds the content and returns `{id}`. A near-duplicate (cosine ≥ 0.92 in the same scope) supersedes the older memory instead of adding a new one. |
| `GET /v1/memories?tenant&scope&domain&project&kind` | List memories. |
| `POST /v1/memories/{id}/deprecate` | Deprecate a memory. |
| `POST /v1/memories/search` | Vector search over live memories. |
| `POST /v1/ingest` | `{tenant, domain?, source_uri, title, content, source_type, metadata}`. Used by edge and agents to store transcripts, summaries and notes. Idempotent on content hash. |
| `GET /v1/stats` | Stats. |
| `/mcp` | MCP Streamable HTTP for Hermes. Tools: `knowledge_search`, `compile_context`, `memory_save`, `memory_search`, `ingest_note`. The `tenant` argument is required and checked against the tenants allowed for the agent (resolved from the session via the orchestrator; default `pessoal` + `shared` for the interactive chief). |

Maintenance runs as `kb maintain`, either via cron task or `make kb-maintain`:
- expire `temporary` memories past `expires_at` (default 14d);
- deprecate superseded memories;
- vacuum orphaned chunks.

Storage uses `kb.storage` and resolves `storage://<bucket>/<key>`. The `local` backend is `STORAGE_LOCAL_ROOT` (`data/storage`). The `s3` backend uses `STORAGE_S3_*` (R2 / Railway Bucket). Nothing stores absolute host paths.

## 6. Edge worker (`edge`, Python FastAPI, workers/edge) — §22 agent-edge

| Method + path | Purpose |
|---|---|
| `POST /extract_audio` | `{source: storage://… or upload}` → `storage://media/processing/<id>.wav` (16 kHz mono via ffmpeg) |
| `POST /transcribe` | `{source, language='pt', diarize=false}` → `{transcript, segments[{start,end,text,speaker?}], srt_uri, txt_uri}`. Uses whisper-server at `WHISPER_URL`. |
| `POST /summarize` | `{text \| transcript_uri, kind: summary\|notes\|tasks\|topics\|all, model='local-qwen'}` → structured JSON. Map-reduce over chunks for long text, via LiteLLM. |
| `POST /process_video` | Extract audio, transcribe, then summarize. |
| `POST /embed` | `{texts[]}` → vectors via LiteLLM `embed-local`. |
| `POST /pipeline` | `{source, tenant, domain, ingest:true}`: full chain, then `knowledge /v1/ingest` for the transcript and the summary. |

Media directories live under storage `media/{input,processing,archive}`, and `models/` is on the host. Retention: originals in `archive` are deleted after `MEDIA_RETENTION_DAYS` (30), transcripts and summaries are kept, and `processing` is cleared after each job.

Diarization is the optional extra `diarize` (pyannote, `HF_TOKEN`) and is off by default. It is not installed in the default image, and requests with `diarize=true` without it return 501.

## 7. Sandbox (`sandbox`, workers/sandbox) — §21 / §24.16 isolated coding worker

This is a long-lived container with `sshd` and a non-root user `agent`. It holds the toolchains (git, Go, Node + pnpm, Python + uv, ruff, eslint, tsc, gofmt, make, jq, duckdb) and a `/workspace` volume. It has **no** docker.sock and no host mounts except `data/sandbox/workspace`.

Hermes `terminal.backend: ssh` points at `sandbox`, so every tool command runs here and not in Hermes.

Per-task lifecycle helpers live in `/usr/local/bin`. The orchestrator calls them over SSH:
- `aios-task-start <task_id> <repo_url> [ref]`: clones into `/workspace/tasks/<task_id>` on branch `aios/<task_id>`.
- `aios-task-check <task_id> [cmd…]`: runs the validators and prints JSON `{exit_code, output_tail}`.
- `aios-task-patch <task_id>`: writes `git diff` to `/workspace/patches/<task_id>.patch` and prints its path.
- `aios-task-destroy <task_id>`: removes the task directory.

## 8. Hermes integration (config/hermes, skills/, tools/hermes-hooks/)

- **One Hermes gateway** (RAM). Domain agents are personas: the orchestrator injects the agent's SOUL and policy per run. Interactive `make hermes-shell` runs as `chief`.
- **Skills:** `skills/<category>/<name>/SKILL.md` for every skill in §5:
  - coding: repository_analysis, debugging, code_review, architecture_review, test_generation
  - finance: spreadsheet_analysis, budget_review, expense_categorization
  - research: web_research, source_synthesis
  - projects: project_status, roadmap_update, decision_record
  - productivity: daily_planning, daily_review, weekly_review
  - knowledge: knowledge_capture, memory_hygiene, transcript_to_notes

  Deterministic work goes into `scripts/`.
- **Hooks (§8):**
  - `pre_tool_call` on everything: `orchestrator /v1/permissions/check` → exit 2 blocks. Also blocks reads and writes of `.env`, `*.pem`, `id_*`, `secrets*`.
  - `post_tool_call` on file-edit tools: run the formatter by extension (gofmt / ruff format+check / eslint --fix / tsc --noEmit) in the sandbox.
  - `post_tool_call` on test commands: POST the result as an event.
  - `agent:end` / `session:end` outbound webhook: decision `/v1/hermes-events`.
  - Stop: notify through the orchestrator.
- **MCP:** register `knowledge` at `http://knowledge:8080/mcp` with `Bearer KNOWLEDGE_API_KEY`.
- **Plugin `aios_context`:** `pre_llm_call` injects compiled context for interactive sessions that the orchestrator did not start.
- **Langfuse plugin:** enabled when `LANGFUSE_PUBLIC_KEY` is set.

## 9. Bench (`bench/`, Python) — §24.18

The task suite lives in `bench/tasks/<category>/*.yaml`:
- 10 simple
- 10 medium
- 10 debugging
- 5 refactor
- 5 agentic
- 5 research
- 5 finance
- 5 media

Each task has `prompt`, fixtures, and a deterministic `check` (command / regex / json-schema), or an `llm_judge` rubric. `bench run [--category] [--repeat]` submits the tasks through orchestrator `/v1/tasks` and records them in `bench_runs` (migration 013). `bench report` prints:
- success rate and cost per success by category × model;
- tokens, iterations and tool calls;
- a recommended `routing.yaml` diff.

## 10. LiteLLM virtual keys

There is one key per service, created by `infra/scripts/litellm-keys.sh` with a model allowlist and a budget:
- `HERMES_LITELLM_KEY`: tiers 2–6
- `DECISION_LITELLM_KEY`: local and tier2-cheap
- `KB_LITELLM_KEY`: embed and local plus tier 2
- `ORCHESTRATOR_LITELLM_KEY`: reviewer and validator, tiers 2–6
- `EDGE_LITELLM_KEY`: local-qwen, embed-local and tier 2

Tier 7 needs an approval.
