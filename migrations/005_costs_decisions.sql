-- 005_costs_decisions: cost per successful task (§14), budgets (§15), decision log (§6)
-- LiteLLM keeps its own spend logs in DB 'litellm'; this ledger ties spend to tasks/runs/routes.

CREATE TABLE llm_calls (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  occurred_at     timestamptz NOT NULL DEFAULT now(),
  run_id          uuid REFERENCES agent_runs(id) ON DELETE SET NULL,
  task_id         uuid REFERENCES tasks(id) ON DELETE SET NULL,
  agent_id        uuid REFERENCES agents(id),
  purpose         text,                               -- 'decision', 'plan', 'execute', 'review', 'embed', 'summary'
  model_alias     text NOT NULL,
  provider_model  text,
  tier            smallint CHECK (tier BETWEEN 0 AND 7),
  input_tokens    int NOT NULL DEFAULT 0,
  output_tokens   int NOT NULL DEFAULT 0,
  cache_hit_tokens int NOT NULL DEFAULT 0,
  cost_usd        numeric(12, 6) NOT NULL DEFAULT 0,
  latency_ms      int,
  success         boolean,
  litellm_call_id text UNIQUE,
  metadata        jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX llm_calls_time_brin ON llm_calls USING brin (occurred_at);
CREATE INDEX llm_calls_run ON llm_calls (run_id);

CREATE TABLE budgets (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  scope_type  text NOT NULL CHECK (scope_type IN ('global', 'tenant', 'domain', 'project', 'agent')),
  scope_id    text NOT NULL DEFAULT '*',
  period      text NOT NULL CHECK (period IN ('run', 'day', 'month')),
  limit_usd   numeric(10, 2) NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (scope_type, scope_id, period)
);
INSERT INTO budgets (scope_type, scope_id, period, limit_usd) VALUES
  ('global', '*', 'month', 50.00),
  ('global', '*', 'day', 5.00);

CREATE TABLE decisions (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  occurred_at    timestamptz NOT NULL DEFAULT now(),
  request_id     text NOT NULL,
  request_hash   text NOT NULL,                       -- sha256(state + questions)
  backend        text NOT NULL,                       -- rules | local | jev | openai | cascade
  state          jsonb,
  questions      jsonb NOT NULL,
  answers        jsonb NOT NULL,
  min_confidence real,
  needs_human    boolean NOT NULL DEFAULT false,
  latency_ms     int,
  input_tokens   int NOT NULL DEFAULT 0,
  cost_usd       numeric(12, 6) NOT NULL DEFAULT 0,
  run_id         uuid REFERENCES agent_runs(id) ON DELETE SET NULL,
  task_id        uuid REFERENCES tasks(id) ON DELETE SET NULL
);
CREATE INDEX decisions_hash ON decisions (request_hash);
CREATE INDEX decisions_time_brin ON decisions USING brin (occurred_at);

-- §14 headline metric: cost per successfully completed task, by task type and final model
CREATE VIEW v_cost_per_successful_task AS
  SELECT t.task_type,
         r.model,
         count(DISTINCT t.id) FILTER (WHERE t.status = 'done')           AS tasks_done,
         count(DISTINCT t.id)                                             AS tasks_total,
         sum(r.cost_usd)                                                  AS total_cost_usd,
         sum(r.cost_usd) / nullif(count(DISTINCT t.id) FILTER (WHERE t.status = 'done'), 0) AS cost_per_success_usd,
         avg(r.iterations)                                                AS avg_iterations,
         sum(r.retries)                                                   AS retries
  FROM tasks t JOIN agent_runs r ON r.task_id = t.id
  GROUP BY 1, 2;

CREATE VIEW v_daily_spend AS
  SELECT date_trunc('day', occurred_at) AS day, model_alias, purpose,
         sum(cost_usd) AS usd, sum(input_tokens) AS in_tok, sum(output_tokens) AS out_tok, count(*) AS calls
  FROM llm_calls GROUP BY 1, 2, 3;
