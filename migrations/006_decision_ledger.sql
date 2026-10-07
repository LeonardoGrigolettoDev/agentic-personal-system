-- 006_decision_ledger: the Decision Service's run ledger for routing, budget and escalation (CONTRACTS §2).
-- Hermes owns execution and tasks (Kanban); a "run" here is the policy/budget view of one Hermes session.

ALTER TABLE agent_runs
  ADD COLUMN tenant               tenant,
  ADD COLUMN domain               domain,
  ADD COLUMN task_type            text,
  ADD COLUMN complexity           text,
  ADD COLUMN hermes_task_id       text,            -- Hermes task / Kanban card id
  ADD COLUMN parent_session_id    text,            -- delegated subagent -> parent session
  ADD COLUMN start_model          text,
  ADD COLUMN route_reason         text,
  ADD COLUMN consecutive_failures int NOT NULL DEFAULT 0,
  ADD COLUMN repairs              int NOT NULL DEFAULT 0,
  ADD COLUMN escalations          int NOT NULL DEFAULT 0,
  ADD COLUMN llm_calls            int NOT NULL DEFAULT 0,
  ADD COLUMN max_cost_usd         numeric(10, 4),
  ADD COLUMN max_tokens           int,
  ADD COLUMN max_iterations       int,
  ADD COLUMN deadline             timestamptz,
  ADD COLUMN summary              text,
  ADD COLUMN updated_at           timestamptz NOT NULL DEFAULT now();

ALTER TABLE agent_runs DROP CONSTRAINT agent_runs_status_check;
ALTER TABLE agent_runs ADD CONSTRAINT agent_runs_status_check
  CHECK (status IN ('running', 'succeeded', 'failed', 'cancelled', 'escalated', 'blocked'));
CREATE UNIQUE INDEX agent_runs_session_uq ON agent_runs (session_id) WHERE session_id IS NOT NULL;
CREATE INDEX agent_runs_stats ON agent_runs (task_type, start_model) WHERE status IN ('succeeded', 'failed');
CREATE TRIGGER agent_runs_upd BEFORE UPDATE ON agent_runs FOR EACH ROW EXECUTE FUNCTION set_updated_at();

ALTER TABLE llm_calls
  ADD COLUMN session_id        text,
  ADD COLUMN request_key       text UNIQUE;          -- Hermes api_request_id: makes /v1/usage idempotent
CREATE INDEX llm_calls_session ON llm_calls (session_id);

-- one row per gate decision (§7): the evidence and what was decided
CREATE TABLE gate_events (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  occurred_at  timestamptz NOT NULL DEFAULT now(),
  run_id       uuid NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
  action       text NOT NULL CHECK (action IN ('done', 'repair', 'escalate', 'fail', 'ask_human')),
  from_model   text NOT NULL,
  to_model     text,
  evidence     jsonb NOT NULL DEFAULT '{}',
  reasons      jsonb NOT NULL DEFAULT '[]',
  decided_by   text NOT NULL DEFAULT 'policy'        -- policy | rules | local | jev | openai | default
);
CREATE INDEX gate_events_run ON gate_events (run_id, occurred_at);
CREATE INDEX gate_events_time_brin ON gate_events USING brin (occurred_at);

ALTER TABLE approvals
  ADD COLUMN kind       text NOT NULL DEFAULT 'generic' CHECK (kind IN ('generic', 'tier', 'action', 'spend')),
  ADD COLUMN session_id text,
  ADD COLUMN expires_at timestamptz;
CREATE INDEX approvals_session ON approvals (session_id) WHERE status = 'approved';

-- §14 headline metric, now over the ledger: cost per successfully completed run, by task type and start model
DROP VIEW v_cost_per_successful_task;
CREATE VIEW v_cost_per_successful_task AS
  SELECT task_type,
         start_model,
         count(*)                                                    AS runs,
         count(*) FILTER (WHERE status = 'succeeded')                AS succeeded,
         count(*) FILTER (WHERE status = 'succeeded' AND escalations = 0) AS solved_without_escalation,
         round(avg((status = 'succeeded')::int)::numeric, 3)         AS success_rate,
         sum(cost_usd)                                               AS total_cost_usd,
         sum(cost_usd) / nullif(count(*) FILTER (WHERE status = 'succeeded'), 0) AS cost_per_success_usd,
         avg(iterations)                                             AS avg_iterations,
         avg(repairs)                                                AS avg_repairs,
         avg(escalations)                                            AS avg_escalations
  FROM agent_runs
  WHERE status IN ('succeeded', 'failed') AND task_type IS NOT NULL
  GROUP BY 1, 2;
