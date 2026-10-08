-- 013_bench: V1 test battery results (docs/CONTRACTS.md §7, docs/ARCHITECTURE.md §24.18 and §14).
-- One row per bench task execution (`bench run`). Cost, tokens, tier and loop counters are copied from the
-- Decision Service ledger (agent_runs, keyed by session_id) when the task finishes, so a row stays meaningful
-- even after the ledger is pruned. `success` is the bench check's verdict; rows with `error` set are
-- infrastructure failures (Hermes/sandbox/judge unavailable) and are excluded from success and cost metrics.

CREATE TABLE bench_runs (
  id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  run_at              timestamptz NOT NULL DEFAULT now(),
  batch_id            text NOT NULL,                    -- one `bench run` invocation
  task_key            text NOT NULL,
  category            text NOT NULL CHECK (category IN
                        ('simple', 'medium', 'debugging', 'refactor', 'agentic', 'research', 'finance', 'media')),
  repeat_index        int NOT NULL DEFAULT 1 CHECK (repeat_index >= 1),
  session_id          text NOT NULL UNIQUE,             -- bench-<key>-<n>-<ts>: Hermes session = ledger run
  hermes_run_id       text,
  hermes_status       text,                             -- completed | failed | cancelled | interrupted | timeout
  hermes_profile      text,                             -- profile that served the run (default = chief)
  tenant              tenant NOT NULL,
  agent               text,                             -- task's target agent (null = chief routes)
  requested_model     text,                             -- `bench run --model`
  start_model         text,                             -- ledger start_model (what routing chose)
  final_model         text,                             -- ledger model after escalations
  tier                smallint CHECK (tier BETWEEN 0 AND 7),
  task_type           text,                             -- router's classification
  expected_task_type  text,                             -- ground truth from the task file
  domain              domain,
  expected_domain     domain,
  complexity          text,
  success             boolean NOT NULL,
  check_type          text NOT NULL CHECK (check_type IN ('command', 'regex', 'json_schema', 'llm_judge')),
  check_detail        jsonb NOT NULL DEFAULT '{}',
  input_tokens        bigint NOT NULL DEFAULT 0,
  output_tokens       bigint NOT NULL DEFAULT 0,
  cache_read_tokens   bigint NOT NULL DEFAULT 0,
  cost_usd            numeric(12, 6) NOT NULL DEFAULT 0,
  iterations          int NOT NULL DEFAULT 0,
  tool_calls          int,                              -- from the Hermes event stream (null = not observed)
  repairs             int NOT NULL DEFAULT 0,
  escalations         int NOT NULL DEFAULT 0,
  duration_ms         int NOT NULL CHECK (duration_ms >= 0),
  router_task_type_ok boolean,                          -- null when the task has no expected_task_type
  router_domain_ok    boolean,                          -- null when the task has no expected_domain
  error               text
);
CREATE INDEX bench_runs_time ON bench_runs (run_at DESC);
CREATE INDEX bench_runs_category_model ON bench_runs (category, start_model);
CREATE INDEX bench_runs_task_type_model ON bench_runs ((coalesce(expected_task_type, task_type)), start_model);

-- §14 cost per successful task by category x start model (infrastructure errors excluded)
CREATE VIEW v_bench_summary AS
  SELECT category,
         start_model,
         count(*)                                                     AS runs,
         count(*) FILTER (WHERE success)                              AS succeeded,
         round(avg(success::int)::numeric, 3)                         AS success_rate,
         sum(cost_usd)                                                AS total_cost_usd,
         sum(cost_usd) / nullif(count(*) FILTER (WHERE success), 0)   AS cost_per_success_usd,
         avg(input_tokens + output_tokens)                            AS avg_tokens,
         avg(iterations)                                              AS avg_iterations,
         round(avg((escalations > 0)::int)::numeric, 3)               AS escalation_rate,
         round(avg(router_task_type_ok::int)::numeric, 3)             AS router_task_type_accuracy,
         round(avg(router_domain_ok::int)::numeric, 3)                AS router_domain_accuracy
  FROM bench_runs
  WHERE error IS NULL
  GROUP BY 1, 2;

GRANT SELECT ON bench_runs, v_bench_summary TO aios_reader;
