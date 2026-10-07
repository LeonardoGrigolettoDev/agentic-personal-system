-- 002_tasks_runs_events: every trigger (cron / webhook / manual) becomes a task (§17); runs + event log (§11 Eventos)

CREATE TABLE tasks (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id      uuid REFERENCES projects(id) ON DELETE CASCADE,
  parent_task_id  uuid REFERENCES tasks(id) ON DELETE SET NULL,
  tenant          tenant NOT NULL DEFAULT 'shared',
  domain          domain,
  trigger         text NOT NULL DEFAULT 'manual' CHECK (trigger IN ('manual', 'cron', 'webhook', 'event', 'agent')),
  task_type       text,                               -- e.g. 'debugging', 'refactor', 'summary' (§14)
  title           text NOT NULL,
  description     text,
  status          text NOT NULL DEFAULT 'todo'
                  CHECK (status IN ('todo', 'in_progress', 'blocked', 'done', 'failed', 'cancelled')),
  priority        smallint NOT NULL DEFAULT 3 CHECK (priority BETWEEN 1 AND 5),
  complexity      text CHECK (complexity IN ('trivial', 'simple', 'medium', 'hard', 'critical')),
  assigned_agent_id uuid REFERENCES agents(id),
  -- budgets (§15): enforced by the orchestrator, recorded here for audit
  token_budget    jsonb NOT NULL DEFAULT '{"total": 30000, "decision": 1000, "retrieval": 3000, "execution": 20000, "final": 6000}',
  max_iterations  int NOT NULL DEFAULT 8,
  max_cost_usd    numeric(10, 4) NOT NULL DEFAULT 0.50,
  deadline        timestamptz,
  created_by      text NOT NULL DEFAULT 'user',
  metadata        jsonb NOT NULL DEFAULT '{}',
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX tasks_project_status ON tasks (project_id, status);
CREATE INDEX tasks_open ON tasks (status, priority) WHERE status IN ('todo', 'in_progress', 'blocked');
CREATE TRIGGER tasks_upd BEFORE UPDATE ON tasks FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE agent_runs (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_id       uuid NOT NULL REFERENCES agents(id),
  task_id        uuid REFERENCES tasks(id) ON DELETE SET NULL,
  session_id     text,                                -- Hermes session id
  status         text NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'succeeded', 'failed', 'cancelled', 'escalated')),
  model          text,                                -- final LiteLLM alias
  tier           smallint CHECK (tier BETWEEN 0 AND 7),
  iterations     int NOT NULL DEFAULT 0,
  retries        int NOT NULL DEFAULT 0,
  tool_calls     int NOT NULL DEFAULT 0,
  input_tokens   bigint NOT NULL DEFAULT 0,
  output_tokens  bigint NOT NULL DEFAULT 0,
  cache_hit_tokens bigint NOT NULL DEFAULT 0,
  cost_usd       numeric(12, 6) NOT NULL DEFAULT 0,
  started_at     timestamptz NOT NULL DEFAULT now(),
  ended_at       timestamptz,
  error          text,
  metadata       jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX agent_runs_agent_started ON agent_runs (agent_id, started_at DESC);
CREATE INDEX agent_runs_task ON agent_runs (task_id);

CREATE TABLE events (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  occurred_at     timestamptz NOT NULL DEFAULT now(),
  source          text NOT NULL,                      -- 'hermes', 'decision', 'cron', 'user', ...
  type            text NOT NULL,                      -- 'agent:end', 'decision.made', 'project.decision', ...
  tenant          tenant,
  project_id      uuid REFERENCES projects(id) ON DELETE SET NULL,
  task_id         uuid REFERENCES tasks(id) ON DELETE SET NULL,
  run_id          uuid REFERENCES agent_runs(id) ON DELETE SET NULL,
  agent_id        uuid REFERENCES agents(id) ON DELETE SET NULL,
  idempotency_key text UNIQUE,
  payload         jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX events_type_time ON events (type, occurred_at DESC);
CREATE INDEX events_time_brin ON events USING brin (occurred_at);
