package ledger

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"aios/decision/internal/policy"
	"github.com/jackc/pgx/v5"
)

// SyncAgents upserts agents/*/agent.yaml into 'agents' and rebuilds their 'permissions' rows,
// so the database mirrors the files (the files stay the source of truth).
func (db *DB) SyncAgents(ctx context.Context, agents map[string]policy.Agent) error {
	tx, err := db.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	for slug, a := range agents {
		cfg, _ := json.Marshal(a)
		var id string
		err := tx.QueryRow(ctx, `
			INSERT INTO agents (slug, name, domain, default_model, max_tier, max_cost_per_run_usd, config)
			VALUES ($1, $2, $3::domain, $4, $5, $6, $7)
			ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name, domain = EXCLUDED.domain,
			  default_model = EXCLUDED.default_model, max_tier = EXCLUDED.max_tier,
			  max_cost_per_run_usd = EXCLUDED.max_cost_per_run_usd, config = EXCLUDED.config, enabled = true
			RETURNING id::text`, slug, a.Name, a.Domain, a.DefaultModel, a.MaxTier, a.MaxCostPerRun, cfg).Scan(&id)
		if err != nil {
			return fmt.Errorf("sync agent %s: %w", slug, err)
		}
		if _, err := tx.Exec(ctx, `DELETE FROM permissions WHERE agent_id = $1`, id); err != nil {
			return err
		}
		type perm struct{ typ, res, effect string }
		var perms []perm
		for _, d := range a.AllowedDomains {
			perms = append(perms, perm{"domain", d, "allow"})
		}
		for _, d := range a.DenyDomains {
			perms = append(perms, perm{"domain", d, "deny"})
		}
		for _, t := range a.AllowedTools {
			perms = append(perms, perm{"tool", t, "allow"})
		}
		for _, t := range a.AllowedTenants {
			perms = append(perms, perm{"tenant", t, "allow"})
		}
		for _, p := range perms {
			if _, err := tx.Exec(ctx, `INSERT INTO permissions (agent_id, resource_type, resource, effect) VALUES ($1, $2, $3, $4)
				ON CONFLICT DO NOTHING`, id, p.typ, p.res, p.effect); err != nil {
				return fmt.Errorf("sync permissions %s: %w", slug, err)
			}
		}
	}
	return tx.Commit(ctx)
}

type Approval struct {
	ID        string          `json:"id"`
	Kind      string          `json:"kind"`
	SessionID string          `json:"session_id,omitempty"`
	Agent     string          `json:"agent,omitempty"`
	Request   json.RawMessage `json:"request"`
	Status    string          `json:"status"`
	DecidedBy string          `json:"decided_by,omitempty"`
	CreatedAt time.Time       `json:"created_at"`
}

// CreateApproval opens a pending approval for a run (deduplicated per session + kind + target model).
func (db *DB) CreateApproval(ctx context.Context, run *Run, kind string, request map[string]any) (string, error) {
	req, _ := json.Marshal(request)
	var id string
	err := db.pool.QueryRow(ctx, `
		SELECT id::text FROM approvals WHERE session_id = $1 AND kind = $2 AND status = 'pending'
		  AND request->>'model' IS NOT DISTINCT FROM $3::jsonb->>'model' LIMIT 1`, run.SessionID, kind, req).Scan(&id)
	if err == nil {
		return id, nil
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return "", err
	}
	err = db.pool.QueryRow(ctx, `INSERT INTO approvals (run_id, agent_id, kind, session_id, request, expires_at)
		VALUES ($1, (SELECT agent_id FROM agent_runs WHERE id = $1), $2, $3, $4, now() + interval '7 days')
		RETURNING id::text`, run.ID, kind, run.SessionID, req).Scan(&id)
	return id, err
}

// ResolveApproval approves or rejects; an approved 'tier' approval also moves the run to that model.
func (db *DB) ResolveApproval(ctx context.Context, id string, approve bool, by string, tierOf func(string) int) (*Approval, error) {
	status := "rejected"
	if approve {
		status = "approved"
	}
	tx, err := db.pool.Begin(ctx)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx)
	var a Approval
	err = tx.QueryRow(ctx, `UPDATE approvals SET status = $2, decided_by = $3, decided_at = now()
		WHERE id = $1 AND status = 'pending' RETURNING id::text, kind, coalesce(session_id,''), request, status, decided_by, created_at`,
		id, status, by).Scan(&a.ID, &a.Kind, &a.SessionID, &a.Request, &a.Status, &a.DecidedBy, &a.CreatedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, ErrNotFound
	}
	if err != nil {
		return nil, err
	}
	if approve && a.Kind == "tier" {
		var req struct {
			Model string `json:"model"`
		}
		_ = json.Unmarshal(a.Request, &req)
		if req.Model != "" {
			if _, err := tx.Exec(ctx, `UPDATE agent_runs SET model = $2, tier = $3, status = 'running',
				escalations = escalations + 1, consecutive_failures = 0 WHERE session_id = $1`,
				a.SessionID, req.Model, tierOf(req.Model)); err != nil {
				return nil, err
			}
		}
	} else if !approve {
		if _, err := tx.Exec(ctx, `UPDATE agent_runs SET status = 'failed', ended_at = coalesce(ended_at, now())
			WHERE session_id = $1 AND status = 'blocked'`, a.SessionID); err != nil {
			return nil, err
		}
	}
	return &a, tx.Commit(ctx)
}

// HasApproval reports whether a session has an approved 'tier' approval for a model.
func (db *DB) HasApproval(ctx context.Context, sessionID, model string) (bool, error) {
	var ok bool
	err := db.pool.QueryRow(ctx, `SELECT EXISTS (SELECT 1 FROM approvals WHERE session_id = $1 AND kind = 'tier'
		AND status = 'approved' AND request->>'model' = $2 AND (expires_at IS NULL OR expires_at > now()))`,
		sessionID, model).Scan(&ok)
	return ok, err
}

func (db *DB) ListApprovals(ctx context.Context, status string) ([]Approval, error) {
	rows, err := db.pool.Query(ctx, `
		SELECT p.id::text, p.kind, coalesce(p.session_id,''), coalesce(a.slug,''), p.request, p.status,
		       coalesce(p.decided_by,''), p.created_at
		FROM approvals p LEFT JOIN agents a ON a.id = p.agent_id
		WHERE ($1 = '' OR p.status = $1) ORDER BY p.created_at DESC LIMIT 200`, status)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, func(row pgx.CollectableRow) (Approval, error) {
		var a Approval
		return a, row.Scan(&a.ID, &a.Kind, &a.SessionID, &a.Agent, &a.Request, &a.Status, &a.DecidedBy, &a.CreatedAt)
	})
}

// Reports answer the §18 questions. Keys are the /v1/reports/{name} names.
var Reports = map[string]string{
	// cost per successful task by task type x start model (the headline metric, §14)
	"costs": `SELECT * FROM v_cost_per_successful_task ORDER BY task_type, cost_per_success_usd NULLS LAST`,
	// which model solves the most (final model of succeeded runs)
	"models": `SELECT model, count(*) FILTER (WHERE status = 'succeeded') AS solved, count(*) AS runs,
	             round(avg((status = 'succeeded')::int)::numeric, 3) AS success_rate, sum(cost_usd) AS cost_usd
	           FROM agent_runs WHERE status IN ('succeeded','failed') GROUP BY 1 ORDER BY solved DESC`,
	// which agent costs the most
	"agents": `SELECT a.slug AS agent, count(*) AS runs, sum(r.cost_usd) AS cost_usd, avg(r.cost_usd) AS avg_cost_usd,
	             sum(r.input_tokens + r.output_tokens) AS tokens
	           FROM agent_runs r JOIN agents a ON a.id = r.agent_id GROUP BY 1 ORDER BY cost_usd DESC`,
	// which skills burn the most tokens (skills reported by the Hermes plugin in usage metadata)
	"skills": `SELECT s.skill, count(*) AS calls, sum(c.input_tokens + c.output_tokens) AS tokens, sum(c.cost_usd) AS cost_usd
	           FROM llm_calls c CROSS JOIN LATERAL jsonb_array_elements_text(coalesce(c.metadata->'skills','[]')) AS s(skill)
	           GROUP BY 1 ORDER BY tokens DESC`,
	// which workflows loop the most
	"loops": `SELECT coalesce(task_type,'(unclassified)') AS task_type, count(*) AS runs, avg(repairs) AS avg_repairs,
	            max(repairs) AS max_repairs, count(*) FILTER (WHERE repairs >= 3) AS runs_with_3plus_repairs,
	            avg(iterations) AS avg_iterations
	          FROM agent_runs GROUP BY 1 ORDER BY avg_repairs DESC`,
	// which route has the best cost/success
	"routes": `SELECT coalesce(domain::text,'-') AS domain, coalesce(task_type,'-') AS task_type,
	             coalesce(complexity,'-') AS complexity, start_model, count(*) AS runs,
	             round(avg((status = 'succeeded')::int)::numeric, 3) AS success_rate,
	             sum(cost_usd) / nullif(count(*) FILTER (WHERE status = 'succeeded'), 0) AS cost_per_success_usd
	           FROM agent_runs WHERE status IN ('succeeded','failed') GROUP BY 1, 2, 3, 4
	           ORDER BY cost_per_success_usd NULLS LAST`,
	// are we escalating too much
	"escalations": `SELECT date_trunc('day', g.occurred_at)::date AS day, count(*) FILTER (WHERE g.action = 'escalate') AS escalations,
	                  count(*) FILTER (WHERE g.action = 'ask_human') AS asked_human, count(*) AS gates,
	                  round(avg((g.action = 'escalate')::int)::numeric, 3) AS escalation_rate,
	                  array_agg(DISTINCT g.from_model || '->' || coalesce(g.to_model,'-'))
	                    FILTER (WHERE g.action = 'escalate') AS paths
	                FROM gate_events g GROUP BY 1 ORDER BY 1 DESC LIMIT 60`,
	// spend per day / model / purpose
	"spend": `SELECT * FROM v_daily_spend ORDER BY day DESC, usd DESC LIMIT 200`,
}

// Report runs a named report and returns rows as JSON-ready maps.
func (db *DB) Report(ctx context.Context, name string) ([]map[string]any, error) {
	q, ok := Reports[name]
	if !ok {
		return nil, ErrNotFound
	}
	rows, err := db.pool.Query(ctx, q)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, pgx.RowToMap)
}
