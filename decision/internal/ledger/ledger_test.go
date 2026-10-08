package ledger

import (
	"context"
	"errors"
	"os"
	"testing"
	"time"

	"aios/decision/internal/policy"
	"github.com/jackc/pgx/v5/pgxpool"
)

// Integration test against a migrated database (make test-integration sets it up):
//
//	DECISION_TEST_DATABASE_URL=postgresql://aios@127.0.0.1:55432/aios go test ./internal/ledger/
func testDB(t *testing.T) (*DB, *policy.Policy) {
	t.Helper()
	url := os.Getenv("DECISION_TEST_DATABASE_URL")
	if url == "" {
		t.Skip("DECISION_TEST_DATABASE_URL not set")
	}
	pool, err := pgxpool.New(context.Background(), url)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(pool.Close)
	pol, err := policy.Load("../../../config/routing.yaml", "../../../agents")
	if err != nil {
		t.Fatal(err)
	}
	db := New(pool, "America/Sao_Paulo")
	if err := db.SyncAgents(context.Background(), pol.Agents); err != nil {
		t.Fatalf("sync agents: %v", err)
	}
	return db, pol
}

func TestLedgerLifecycle(t *testing.T) {
	db, pol := testDB(t)
	ctx := context.Background()
	session := "it-" + time.Now().Format("150405.000000")

	if _, err := db.GetRun(ctx, session); !errors.Is(err, ErrNotFound) {
		t.Fatalf("missing run: %v", err)
	}
	run, err := db.CreateRun(ctx, Run{SessionID: session, Agent: "engineering", Tenant: "nitro", Domain: "engineering",
		TaskType: "debugging", Complexity: "medium", Model: "tier3-code", Tier: 3, RouteReason: "test",
		MaxCostUSD: 0.5, MaxTokens: 30000, MaxIterations: 8, Summary: "bug no checkout"})
	if err != nil || run.Agent != "engineering" || run.StartModel != "tier3-code" || run.Status != "running" {
		t.Fatalf("create: %+v %v", run, err)
	}
	again, err := db.CreateRun(ctx, Run{SessionID: session, Agent: "chief", Model: "tier2-cheap", Tier: 2})
	if err != nil || again.Agent != "engineering" {
		t.Fatalf("second create must keep the first run: %+v %v", again, err)
	}

	u := UsageRow{SessionID: session, RequestKey: session + "-r1", Model: "tier3-code", Tier: 3, InputTokens: 1000,
		OutputTokens: 200, CacheTokens: 100, CostUSD: pol.Cost("tier3-code", 1000, 200, 100), Success: true,
		Metadata: map[string]any{"skills": []string{"coding/debugging"}}}
	applied, run, err := db.RecordUsage(ctx, u)
	if err != nil || !applied || run.LLMCalls != 1 || run.InputTokens != 1000 || run.CostUSD <= 0 {
		t.Fatalf("usage: %v %+v %v", applied, run, err)
	}
	if applied, run, _ = db.RecordUsage(ctx, u); applied || run.LLMCalls != 1 {
		t.Fatalf("usage must be idempotent: %v %+v", applied, run)
	}

	spend, err := db.GlobalSpend(ctx)
	if err != nil || len(spend) != 2 || spend[0].LimitUSD <= 0 || spend[0].SpentUSD <= 0 {
		t.Fatalf("global spend: %+v %v", spend, err)
	}

	g := policy.Gate{Action: policy.ActionRepair, Reasons: []string{"tests failed"}}
	run, err = db.ApplyGate(ctx, run, g, 0, true, map[string]any{"validation": "fail"}, "policy")
	if err != nil || run.Repairs != 1 || run.ConsecutiveFailures != 1 || run.Iterations != 1 {
		t.Fatalf("gate repair: %+v %v", run, err)
	}
	g = policy.Gate{Action: policy.ActionEscalate, NextModel: "tier4-pro", Tier: 4, Reasons: []string{"2 failures"}}
	run, err = db.ApplyGate(ctx, run, g, 4, true, map[string]any{"validation": "fail"}, "policy")
	if err != nil || run.Model != "tier4-pro" || run.Tier != 4 || run.Escalations != 1 || run.ConsecutiveFailures != 0 {
		t.Fatalf("gate escalate: %+v %v", run, err)
	}

	id, created, err := db.CreateApproval(ctx, run, "tier", map[string]any{"model": "tier7-fable"})
	if err != nil || id == "" || !created {
		t.Fatalf("approval: %v", err)
	}
	if dup, again, _ := db.CreateApproval(ctx, run, "tier", map[string]any{"model": "tier7-fable"}); dup != id || again {
		t.Fatalf("pending approval must be deduplicated: %s vs %s", dup, id)
	}
	if ok, _ := db.HasApproval(ctx, session, "tier7-fable"); ok {
		t.Fatal("pending approval is not approved")
	}
	if pending, err := db.ListApprovals(ctx, "pending"); err != nil || len(pending) == 0 {
		t.Fatalf("list approvals: %v %v", pending, err)
	}
	if _, err := db.ResolveApproval(ctx, id, true, "test", pol.Tier); err != nil {
		t.Fatalf("resolve approval: %v", err)
	}
	if ok, _ := db.HasApproval(ctx, session, "tier7-fable"); !ok {
		t.Fatal("approved approval not found")
	}
	if run, _ = db.GetRun(ctx, session); run.Model != "tier7-fable" || run.Tier != 7 {
		t.Fatalf("approved tier must move the run: %+v", run)
	}
	if _, err := db.ResolveApproval(ctx, id, false, "test", pol.Tier); !errors.Is(err, ErrNotFound) {
		t.Fatalf("resolving twice: %v", err)
	}

	run, err = db.FinishRun(ctx, session, "succeeded", "debugging")
	if err != nil || run.Status != "succeeded" || run.EndedAt == nil {
		t.Fatalf("finish: %+v %v", run, err)
	}
	// first close wins (a later Hermes finalize can't rewrite the verdict); failed evidence always sticks
	if run, err = db.FinishRun(ctx, session, "cancelled", ""); err != nil || run.Status != "succeeded" {
		t.Fatalf("second close rewrote the outcome: %+v %v", run, err)
	}
	stats, err := db.Stats(ctx, "debugging")
	if err != nil || len(stats) == 0 {
		t.Fatalf("stats: %+v %v", stats, err)
	}

	for name := range Reports {
		if _, err := db.Report(ctx, name); err != nil {
			t.Errorf("report %s: %v", name, err)
		}
	}
	if _, err := db.Report(ctx, "nope"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("unknown report: %v", err)
	}
	if err := db.Ready(ctx, "006_decision_ledger"); err != nil {
		t.Fatalf("ready: %v", err)
	}
}
