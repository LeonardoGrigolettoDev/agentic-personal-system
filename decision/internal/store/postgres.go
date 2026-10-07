// Package store persists decisions/events to Postgres and caches answers in Valkey.
package store

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"

	"aios/decision/internal/decide"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

type DB struct{ pool *pgxpool.Pool }

// Open builds a lazy pool: it does not fail if Postgres is down yet (pool_max_conns etc.
// are honored from the URL).
func Open(ctx context.Context, url string) (*DB, error) {
	cfg, err := pgxpool.ParseConfig(url)
	if err != nil {
		return nil, fmt.Errorf("DATABASE_URL: %w", err)
	}
	cfg.ConnConfig.RuntimeParams["application_name"] = "decision"
	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		return nil, err
	}
	return &DB{pool: pool}, nil
}

func (db *DB) Close() { db.pool.Close() }

// Pool shares the connection pool with the ledger (one pool per process).
func (db *DB) Pool() *pgxpool.Pool { return db.pool }

// Ready pings the DB and checks that the required migration is applied.
func (db *DB) Ready(ctx context.Context, migration string) error {
	if err := db.pool.Ping(ctx); err != nil {
		return fmt.Errorf("postgres: %w", err)
	}
	var ok bool
	err := db.pool.QueryRow(ctx, `SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE version = $1)`, migration).Scan(&ok)
	if err != nil {
		return fmt.Errorf("schema_migrations: %w", err)
	}
	if !ok {
		return fmt.Errorf("migration %s not applied", migration)
	}
	return nil
}

// DecisionRow is one row of the 'decisions' table (migrations/005_costs_decisions.sql).
type DecisionRow struct {
	RequestID     string
	RequestHash   string
	Backend       string
	State         any
	Questions     []decide.Question
	Answers       []decide.Answer
	MinConfidence float64
	NeedsHuman    bool
	LatencyMS     int64
	InputTokens   int
	CostUSD       float64
	RunID, TaskID string
}

const insertDecision = `INSERT INTO decisions
  (request_id, request_hash, backend, state, questions, answers, min_confidence, needs_human,
   latency_ms, input_tokens, cost_usd, run_id, task_id)
  VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)`

const fkViolation = "23503"

// InsertDecision logs a decision. If run_id/task_id reference rows that don't exist (yet),
// the row is kept with those links cleared rather than lost.
func (db *DB) InsertDecision(ctx context.Context, r DecisionRow) error {
	state, err := json.Marshal(r.State)
	if err != nil {
		return err
	}
	questions, _ := json.Marshal(r.Questions)
	answers, _ := json.Marshal(r.Answers)
	exec := func(runID, taskID any) error {
		_, err := db.pool.Exec(ctx, insertDecision, r.RequestID, r.RequestHash, r.Backend, state, questions, answers,
			r.MinConfidence, r.NeedsHuman, r.LatencyMS, r.InputTokens, r.CostUSD, runID, taskID)
		return err
	}
	err = exec(nullable(r.RunID), nullable(r.TaskID))
	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) && pgErr.Code == fkViolation {
		err = exec(nil, nil)
	}
	return err
}

// InsertEvent appends to the 'events' log.
func (db *DB) InsertEvent(ctx context.Context, source, typ string, payload json.RawMessage) error {
	_, err := db.pool.Exec(ctx, `INSERT INTO events (source, type, payload) VALUES ($1, $2, $3)`, source, typ, payload)
	return err
}

func nullable(s string) any {
	if s == "" {
		return nil
	}
	return s
}
