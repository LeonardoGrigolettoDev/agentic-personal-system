// Package ledger persists the Decision Service's view of Hermes sessions: runs, usage, gate
// decisions, approvals (migrations/006_decision_ledger.sql). It never executes anything.
package ledger

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"aios/decision/internal/policy"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

var ErrNotFound = errors.New("not found")

type Run struct {
	ID                  string     `json:"run_id"`
	SessionID           string     `json:"session_id"`
	Agent               string     `json:"agent"`
	Tenant              string     `json:"tenant,omitempty"`
	Domain              string     `json:"domain,omitempty"`
	TaskType            string     `json:"task_type,omitempty"`
	Complexity          string     `json:"complexity,omitempty"`
	HermesTaskID        string     `json:"hermes_task_id,omitempty"`
	ParentSessionID     string     `json:"parent_session_id,omitempty"`
	Status              string     `json:"status"`
	Model               string     `json:"model"`
	StartModel          string     `json:"start_model"`
	Tier                int        `json:"tier"`
	RouteReason         string     `json:"route_reason,omitempty"`
	InputTokens         int64      `json:"input_tokens"`
	OutputTokens        int64      `json:"output_tokens"`
	CacheTokens         int64      `json:"cache_read_tokens"`
	CostUSD             float64    `json:"cost_usd"`
	LLMCalls            int        `json:"llm_calls"`
	Iterations          int        `json:"iterations"`
	Repairs             int        `json:"repairs"`
	Escalations         int        `json:"escalations"`
	ConsecutiveFailures int        `json:"consecutive_failures"`
	MaxCostUSD          float64    `json:"max_cost_usd"`
	MaxTokens           int        `json:"max_tokens"`
	MaxIterations       int        `json:"max_iterations"`
	Deadline            *time.Time `json:"deadline,omitempty"`
	Summary             string     `json:"summary,omitempty"`
	StartedAt           time.Time  `json:"started_at"`
	EndedAt             *time.Time `json:"ended_at,omitempty"`
}

// Usage and Limits feed policy.CheckBudget. The token budget counts fresh tokens (uncached input + output):
// an agent loop resends its whole prompt (tool schemas, system prompt) every call, and those prefix
// tokens come back as cache reads; cost already prices them, so they don't also eat the token budget.
func (r *Run) Usage() policy.Usage {
	fresh := max(r.InputTokens-r.CacheTokens, 0) + r.OutputTokens
	return policy.Usage{CostUSD: r.CostUSD, Tokens: int(fresh), Iterations: r.Iterations}
}

func (r *Run) Limits() policy.Limits {
	return policy.Limits{MaxCostUSD: r.MaxCostUSD, MaxTokens: r.MaxTokens, MaxIterations: r.MaxIterations, Deadline: r.Deadline}
}

type DB struct {
	pool *pgxpool.Pool
	tz   string // budget day/month boundaries (e.g. America/Sao_Paulo)
}

func New(pool *pgxpool.Pool, tz string) *DB {
	if tz == "" {
		tz = "UTC"
	}
	return &DB{pool: pool, tz: tz}
}

const runColumns = `r.id::text, r.session_id, a.slug, coalesce(r.tenant::text,''), coalesce(r.domain::text,''),
  coalesce(r.task_type,''), coalesce(r.complexity,''), coalesce(r.hermes_task_id,''), coalesce(r.parent_session_id,''),
  r.status, coalesce(r.model,''), coalesce(r.start_model,''), coalesce(r.tier,0), coalesce(r.route_reason,''),
  r.input_tokens, r.output_tokens, r.cache_hit_tokens, r.cost_usd::float8, r.llm_calls, r.iterations, r.repairs,
  r.escalations, r.consecutive_failures, coalesce(r.max_cost_usd,0)::float8, coalesce(r.max_tokens,0),
  coalesce(r.max_iterations,0), r.deadline, coalesce(r.summary,''), r.started_at, r.ended_at`

func scanRun(row pgx.Row) (*Run, error) {
	var r Run
	err := row.Scan(&r.ID, &r.SessionID, &r.Agent, &r.Tenant, &r.Domain, &r.TaskType, &r.Complexity, &r.HermesTaskID,
		&r.ParentSessionID, &r.Status, &r.Model, &r.StartModel, &r.Tier, &r.RouteReason, &r.InputTokens, &r.OutputTokens,
		&r.CacheTokens, &r.CostUSD, &r.LLMCalls, &r.Iterations, &r.Repairs, &r.Escalations, &r.ConsecutiveFailures,
		&r.MaxCostUSD, &r.MaxTokens, &r.MaxIterations, &r.Deadline, &r.Summary, &r.StartedAt, &r.EndedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, ErrNotFound
	}
	return &r, err
}

func (db *DB) GetRun(ctx context.Context, sessionID string) (*Run, error) {
	return scanRun(db.pool.QueryRow(ctx, `SELECT `+runColumns+` FROM agent_runs r JOIN agents a ON a.id = r.agent_id
		WHERE r.session_id = $1`, sessionID))
}

// CreateRun inserts a run for a session; if one already exists (concurrent route calls), the
// existing run wins and is returned unchanged.
func (db *DB) CreateRun(ctx context.Context, r Run) (*Run, error) {
	_, err := db.pool.Exec(ctx, `
		INSERT INTO agent_runs (agent_id, session_id, tenant, domain, task_type, complexity, hermes_task_id,
		  parent_session_id, status, model, start_model, tier, route_reason, max_cost_usd, max_tokens, max_iterations,
		  deadline, summary)
		VALUES ((SELECT id FROM agents WHERE slug = $1), $2, nullif($3,'')::tenant, nullif($4,'')::domain, nullif($5,''),
		  nullif($6,''), nullif($7,''), nullif($8,''), 'running', $9, $9, $10, $11, $12, $13, $14, $15, left($16, 500))
		ON CONFLICT (session_id) WHERE session_id IS NOT NULL DO NOTHING`,
		r.Agent, r.SessionID, r.Tenant, r.Domain, r.TaskType, r.Complexity, r.HermesTaskID, r.ParentSessionID,
		r.Model, r.Tier, r.RouteReason, r.MaxCostUSD, r.MaxTokens, r.MaxIterations, r.Deadline, r.Summary)
	if err != nil {
		return nil, fmt.Errorf("create run: %w", err)
	}
	return db.GetRun(ctx, r.SessionID)
}

type UsageRow struct {
	SessionID    string
	RequestKey   string
	Model        string
	Tier         int
	Purpose      string
	InputTokens  int
	OutputTokens int
	CacheTokens  int
	CostUSD      float64
	LatencyMS    int
	Success      bool
	Metadata     map[string]any
}

// RecordUsage appends one LLM call and rolls it into the run. Idempotent on RequestKey:
// a replayed call returns (false, current run).
func (db *DB) RecordUsage(ctx context.Context, u UsageRow) (bool, *Run, error) {
	meta, _ := json.Marshal(u.Metadata)
	tx, err := db.pool.Begin(ctx)
	if err != nil {
		return false, nil, err
	}
	defer tx.Rollback(ctx)
	tag, err := tx.Exec(ctx, `
		INSERT INTO llm_calls (run_id, agent_id, session_id, purpose, model_alias, tier, input_tokens, output_tokens,
		  cache_hit_tokens, cost_usd, latency_ms, success, request_key, metadata)
		SELECT r.id, r.agent_id, $1, nullif($2,''), $3, nullif($4,0), $5, $6, $7, $8, nullif($9,0), $10,
		       nullif($11,''), $12
		FROM agent_runs r WHERE r.session_id = $1
		ON CONFLICT (request_key) DO NOTHING`,
		u.SessionID, u.Purpose, u.Model, u.Tier, u.InputTokens, u.OutputTokens, u.CacheTokens, u.CostUSD, u.LatencyMS,
		u.Success, u.RequestKey, meta)
	if err != nil {
		return false, nil, fmt.Errorf("insert llm_call: %w", err)
	}
	applied := tag.RowsAffected() == 1
	if applied {
		if _, err := tx.Exec(ctx, `UPDATE agent_runs SET input_tokens = input_tokens + $2, output_tokens = output_tokens + $3,
			cache_hit_tokens = cache_hit_tokens + $4, cost_usd = cost_usd + $5, llm_calls = llm_calls + 1
			WHERE session_id = $1`, u.SessionID, u.InputTokens, u.OutputTokens, u.CacheTokens, u.CostUSD); err != nil {
			return false, nil, fmt.Errorf("update run usage: %w", err)
		}
	}
	if err := tx.Commit(ctx); err != nil {
		return false, nil, err
	}
	run, err := db.GetRun(ctx, u.SessionID)
	return applied, run, err
}

// GlobalSpend returns spend against every 'global' row of the budgets table (day / month), with
// period boundaries in the configured time zone.
func (db *DB) GlobalSpend(ctx context.Context) ([]policy.Spend, error) {
	rows, err := db.pool.Query(ctx, `
		SELECT 'global/' || b.period, b.limit_usd::float8,
		       coalesce((SELECT sum(c.cost_usd) FROM llm_calls c
		                 WHERE c.occurred_at >= (date_trunc(CASE b.period WHEN 'day' THEN 'day' ELSE 'month' END,
		                                                    now() AT TIME ZONE $1) AT TIME ZONE $1)), 0)::float8
		FROM budgets b WHERE b.scope_type = 'global' AND b.period IN ('day', 'month')`, db.tz)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, func(row pgx.CollectableRow) (policy.Spend, error) {
		var s policy.Spend
		return s, row.Scan(&s.Scope, &s.LimitUSD, &s.SpentUSD)
	})
}

// Stats returns per-start-model performance for a task type over the last 90 days.
func (db *DB) Stats(ctx context.Context, taskType string) ([]policy.Stat, error) {
	rows, err := db.pool.Query(ctx, `
		SELECT start_model, count(*),
		       count(*) FILTER (WHERE status = 'succeeded' AND escalations = 0),
		       coalesce(sum(cost_usd) / nullif(count(*) FILTER (WHERE status = 'succeeded'), 0), 0)::float8
		FROM agent_runs
		WHERE task_type = $1 AND start_model IS NOT NULL AND status IN ('succeeded', 'failed')
		  AND started_at > now() - interval '90 days'
		GROUP BY 1`, taskType)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, func(row pgx.CollectableRow) (policy.Stat, error) {
		var s policy.Stat
		return s, row.Scan(&s.Model, &s.Runs, &s.Solved, &s.CostPerSuccess)
	})
}

// ApplyGate records a gate decision and updates the run atomically.
func (db *DB) ApplyGate(ctx context.Context, run *Run, g policy.Gate, nextTier int, failed bool, evidence any, decidedBy string) (*Run, error) {
	ev, _ := json.Marshal(evidence)
	reasons, _ := json.Marshal(g.Reasons)
	tx, err := db.pool.Begin(ctx)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx)
	if _, err := tx.Exec(ctx, `INSERT INTO gate_events (run_id, action, from_model, to_model, evidence, reasons, decided_by)
		VALUES ($1, $2, $3, nullif($4,''), $5, $6, $7)`, run.ID, g.Action, run.Model, g.NextModel, ev, reasons, decidedBy); err != nil {
		return nil, fmt.Errorf("insert gate_event: %w", err)
	}
	_, err = tx.Exec(ctx, `
		UPDATE agent_runs SET
		  iterations = iterations + 1,
		  consecutive_failures = CASE WHEN $2 = 'escalate' THEN 0 WHEN $3 THEN consecutive_failures + 1 ELSE 0 END,
		  repairs = repairs + (CASE WHEN $2 = 'repair' THEN 1 ELSE 0 END),
		  escalations = escalations + (CASE WHEN $2 = 'escalate' THEN 1 ELSE 0 END),
		  model = CASE WHEN $2 = 'escalate' THEN $4 ELSE model END,
		  tier = CASE WHEN $2 = 'escalate' THEN $5 ELSE tier END,
		  status = CASE $2 WHEN 'fail' THEN 'failed' WHEN 'ask_human' THEN 'blocked' ELSE 'running' END,
		  ended_at = CASE WHEN $2 = 'fail' THEN now() ELSE ended_at END
		WHERE id = $1`, run.ID, g.Action, failed, g.NextModel, nextTier)
	if err != nil {
		return nil, fmt.Errorf("update run gate: %w", err)
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, err
	}
	return db.GetRun(ctx, run.SessionID)
}

// FinishRun records a run's outcome (succeeded | failed | cancelled). The first close wins: a later close
// (Hermes' session finalize after the bench verdict, a gateway shutdown) never rewrites it, except that
// 'failed' evidence always sticks. ended_at keeps the first close.
func (db *DB) FinishRun(ctx context.Context, sessionID, status, taskType string) (*Run, error) {
	tag, err := db.pool.Exec(ctx, `UPDATE agent_runs SET status = CASE
		  WHEN status = 'failed' THEN status
		  WHEN ended_at IS NOT NULL AND $2 <> 'failed' THEN status
		  ELSE $2 END,
		task_type = coalesce(nullif($3,''), task_type),
		ended_at = coalesce(ended_at, now()) WHERE session_id = $1`, sessionID, status, taskType)
	if err != nil {
		return nil, err
	}
	if tag.RowsAffected() == 0 {
		return nil, ErrNotFound
	}
	return db.GetRun(ctx, sessionID)
}

// Ready pings the DB and checks that the required migration is applied.
func (db *DB) Ready(ctx context.Context, migration string) error {
	var ok bool
	if err := db.pool.QueryRow(ctx, `SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE version = $1)`, migration).Scan(&ok); err != nil {
		return fmt.Errorf("postgres: %w", err)
	}
	if !ok {
		return fmt.Errorf("migration %s not applied", migration)
	}
	return nil
}
