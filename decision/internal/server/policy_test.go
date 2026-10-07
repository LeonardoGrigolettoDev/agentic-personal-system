package server

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"aios/decision/internal/decide"
	"aios/decision/internal/ledger"
	"aios/decision/internal/policy"
)

// memLedger is an in-memory ledger.DB with the same semantics the handlers rely on.
type memLedger struct {
	mu        sync.Mutex
	runs      map[string]*ledger.Run
	keys      map[string]bool
	approvals map[string]*ledger.Approval
	gates     []policy.Gate
	spend     []policy.Spend
}

func newMem() *memLedger {
	return &memLedger{runs: map[string]*ledger.Run{}, keys: map[string]bool{}, approvals: map[string]*ledger.Approval{}}
}

func (m *memLedger) GetRun(_ context.Context, id string) (*ledger.Run, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if r, ok := m.runs[id]; ok {
		c := *r
		return &c, nil
	}
	return nil, ledger.ErrNotFound
}

func (m *memLedger) CreateRun(ctx context.Context, r ledger.Run) (*ledger.Run, error) {
	m.mu.Lock()
	if _, ok := m.runs[r.SessionID]; !ok {
		r.ID, r.Status, r.StartModel, r.StartedAt = "run-"+r.SessionID, "running", r.Model, time.Now()
		m.runs[r.SessionID] = &r
	}
	m.mu.Unlock()
	return m.GetRun(ctx, r.SessionID)
}

func (m *memLedger) RecordUsage(ctx context.Context, u ledger.UsageRow) (bool, *ledger.Run, error) {
	m.mu.Lock()
	applied := !m.keys[u.RequestKey]
	if applied {
		m.keys[u.RequestKey] = true
		r := m.runs[u.SessionID]
		r.InputTokens += int64(u.InputTokens)
		r.OutputTokens += int64(u.OutputTokens)
		r.CostUSD += u.CostUSD
		r.LLMCalls++
	}
	m.mu.Unlock()
	run, err := m.GetRun(ctx, u.SessionID)
	return applied, run, err
}

func (m *memLedger) GlobalSpend(context.Context) ([]policy.Spend, error)  { return m.spend, nil }
func (m *memLedger) Stats(context.Context, string) ([]policy.Stat, error) { return nil, nil }
func (m *memLedger) Report(context.Context, string) ([]map[string]any, error) {
	return nil, ledger.ErrNotFound
}
func (m *memLedger) ListApprovals(context.Context, string) ([]ledger.Approval, error) {
	return nil, nil
}

func (m *memLedger) ApplyGate(ctx context.Context, run *ledger.Run, g policy.Gate, tier int, failed bool, _ any, _ string) (*ledger.Run, error) {
	m.mu.Lock()
	r := m.runs[run.SessionID]
	m.gates = append(m.gates, g)
	r.Iterations++
	switch {
	case g.Action == policy.ActionEscalate:
		r.Model, r.Tier, r.ConsecutiveFailures, r.Escalations = g.NextModel, tier, 0, r.Escalations+1
	case failed:
		r.ConsecutiveFailures++
	default:
		r.ConsecutiveFailures = 0
	}
	if g.Action == policy.ActionRepair {
		r.Repairs++
	}
	switch g.Action {
	case policy.ActionFail:
		r.Status = "failed"
	case policy.ActionAskHuman:
		r.Status = "blocked"
	}
	m.mu.Unlock()
	return m.GetRun(ctx, run.SessionID)
}

func (m *memLedger) FinishRun(ctx context.Context, id, status, _ string) (*ledger.Run, error) {
	m.mu.Lock()
	r, ok := m.runs[id]
	if ok {
		r.Status = status
	}
	m.mu.Unlock()
	if !ok {
		return nil, ledger.ErrNotFound
	}
	return m.GetRun(ctx, id)
}

func (m *memLedger) CreateApproval(_ context.Context, run *ledger.Run, kind string, req map[string]any) (string, error) {
	b, _ := json.Marshal(req)
	m.mu.Lock()
	defer m.mu.Unlock()
	id := "appr-" + run.SessionID
	m.approvals[id] = &ledger.Approval{ID: id, Kind: kind, SessionID: run.SessionID, Request: b, Status: "pending"}
	return id, nil
}

func (m *memLedger) ResolveApproval(_ context.Context, id string, approve bool, by string, tierOf func(string) int) (*ledger.Approval, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	a, ok := m.approvals[id]
	if !ok || a.Status != "pending" {
		return nil, ledger.ErrNotFound
	}
	a.Status, a.DecidedBy = map[bool]string{true: "approved", false: "rejected"}[approve], by
	if approve {
		var req struct{ Model string }
		_ = json.Unmarshal(a.Request, &req)
		r := m.runs[a.SessionID]
		r.Model, r.Tier, r.Status = req.Model, tierOf(req.Model), "running"
	}
	return a, nil
}

func (m *memLedger) HasApproval(_ context.Context, session, model string) (bool, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	for _, a := range m.approvals {
		if a.SessionID == session && a.Status == "approved" && strings.Contains(string(a.Request), model) {
			return true, nil
		}
	}
	return false, nil
}

// classifier answers like the cascade would: domain/task_type/complexity, never research.
type classifier struct{ nextStep string }

func (c classifier) DecideTrace(_ context.Context, req decide.Request) ([]decide.Answer, []decide.Step, error) {
	var out []decide.Answer
	for _, q := range req.Questions {
		a := decide.Answer{Name: q.Name, Type: q.Type, Confidence: 0.95, Backend: "local"}
		switch q.Name {
		case "domain":
			a.Choice = "engineering"
		case "task_type":
			a.Choice = "debugging"
		case "complexity":
			a.Choice = "medium"
		case "next_step":
			a.Choice = c.nextStep
			a.NeedsHuman = c.nextStep == ""
		default:
			a.Choice = "false"
		}
		out = append(out, a)
	}
	return out, []decide.Step{{Backend: "local"}}, nil
}
func (classifier) Plan(decide.Request) ([]string, error) { return []string{"local"}, nil }
func (classifier) Threshold(decide.Request) float64      { return 0.8 }

func policyServer(t *testing.T, mem *memLedger, eng Decider) http.Handler {
	t.Helper()
	pol, err := policy.Load("../../../config/routing.yaml", "../../../agents")
	if err != nil {
		t.Fatal(err)
	}
	return (&Server{Engine: eng, Policy: pol, Ledger: mem, APIKey: "k", DecideTimeout: time.Second,
		Log: slog.New(slog.NewTextHandler(io.Discard, nil))}).Handler()
}

func call(t *testing.T, h http.Handler, method, path, body string) (int, map[string]any) {
	t.Helper()
	req := httptest.NewRequest(method, path, strings.NewReader(body))
	req.Header.Set("Authorization", "Bearer k")
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	var out map[string]any
	_ = json.Unmarshal(rec.Body.Bytes(), &out)
	return rec.Code, out
}

func TestRouteClassifiesAndIsStable(t *testing.T) {
	mem := newMem()
	h := policyServer(t, mem, classifier{})
	code, out := call(t, h, "POST", "/v1/route", `{"session_id":"s1","text":"bug no checkout","tenant":"nitro"}`)
	if code != 200 {
		t.Fatalf("route %d %v", code, out)
	}
	if out["agent"] != "engineering" || out["task_type"] != "debugging" || out["model"] != "tier3-code" || out["validator"] != "command" {
		t.Fatalf("route result %v", out)
	}
	if out["budget"].(map[string]any)["state"] != "ok" {
		t.Fatalf("budget %v", out["budget"])
	}
	_, again := call(t, h, "POST", "/v1/route", `{"session_id":"s1","text":"outra coisa"}`)
	if again["existing"] != true || again["model"] != "tier3-code" {
		t.Fatalf("second route must return the existing run: %v", again)
	}
}

func TestRouteHintsSkipClassificationAndValidate(t *testing.T) {
	h := policyServer(t, newMem(), nil) // no engine: hints + defaults only
	code, out := call(t, h, "POST", "/v1/route", `{"session_id":"s2","agent":"finance","text":"x","task_type":"finance_analysis","complexity":"simple"}`)
	if code != 200 || out["model"] != "tier2-flash" || out["domain"] != "finance" {
		t.Fatalf("%d %v", code, out)
	}
	code, out = call(t, h, "POST", "/v1/route", `{"session_id":"s3","complexity":"insane"}`)
	if code != 400 {
		t.Fatalf("bad complexity accepted: %d %v", code, out)
	}
}

func TestResolveFollowsRunAndApproval(t *testing.T) {
	mem := newMem()
	h := policyServer(t, mem, classifier{})
	_, out := call(t, h, "POST", "/v1/models/resolve", `{"session_id":"nope","requested_model":"tier7-fable"}`)
	if out["model"] != "tier6-opus" {
		t.Fatalf("unrouted fable must be capped: %v", out)
	}
	call(t, h, "POST", "/v1/route", `{"session_id":"s4","text":"bug"}`)
	_, out = call(t, h, "POST", "/v1/models/resolve", `{"session_id":"s4","requested_model":"whatever"}`)
	if out["model"] != "tier3-code" || out["source"] != "run" {
		t.Fatalf("resolve must follow the run: %v", out)
	}
}

func TestUsageIsIdempotentAndBudgets(t *testing.T) {
	mem := newMem()
	h := policyServer(t, mem, classifier{})
	body := `{"session_id":"s5","model":"tier5-sonnet","input_tokens":100000,"output_tokens":20000,"api_request_id":"r1"}`
	_, first := call(t, h, "POST", "/v1/usage", body) // unrouted session -> default run is created
	_, second := call(t, h, "POST", "/v1/usage", body)
	if first["applied"] != true || second["applied"] != false {
		t.Fatalf("idempotency: %v / %v", first["applied"], second["applied"])
	}
	run := second["run"].(map[string]any)
	if run["llm_calls"].(float64) != 1 || run["cost_usd"].(float64) <= 0.39 { // 0.1M*$2 + 0.02M*$10 = $0.40
		t.Fatalf("usage totals %v", run)
	}
	mem.spend = []policy.Spend{{Scope: "global/day", SpentUSD: 5, LimitUSD: 5}}
	_, b := call(t, h, "POST", "/v1/budget/check", `{"session_id":"s5"}`)
	if b["state"] != "exhausted" {
		t.Fatalf("global budget: %v", b)
	}
}

func TestGateLoop(t *testing.T) {
	mem := newMem()
	h := policyServer(t, mem, classifier{nextStep: "repair"})
	call(t, h, "POST", "/v1/route", `{"session_id":"s6","text":"bug","agent":"projects","task_type":"debugging","complexity":"hard"}`)
	// projects: max_tier 5, debugging/hard starts on tier4-pro
	fail := `{"session_id":"s6","evidence":{"validation":"fail","tests":{"exit_code":1,"output_tail":"FAIL TestX"}}}`

	_, g1 := call(t, h, "POST", "/v1/gate", fail)
	if g1["action"] != "repair" || !strings.Contains(g1["message"].(string), "FAIL TestX") {
		t.Fatalf("first failure -> repair with diagnosis: %v", g1)
	}
	_, g2 := call(t, h, "POST", "/v1/gate", fail)
	if g2["action"] != "escalate" || g2["next_model"] != "tier4-k3" {
		t.Fatalf("second failure -> escalate: %v", g2)
	}
	if _, out := call(t, h, "POST", "/v1/models/resolve", `{"session_id":"s6"}`); out["model"] != "tier4-k3" {
		t.Fatalf("escalation must change the per-call model: %v", out)
	}
	call(t, h, "POST", "/v1/gate", fail)
	_, g4 := call(t, h, "POST", "/v1/gate", fail) // k3 fails twice -> sonnet (tier 5 == max) ok
	if g4["action"] != "escalate" || g4["next_model"] != "tier5-sonnet" {
		t.Fatalf("escalate to sonnet: %v", g4)
	}
	call(t, h, "POST", "/v1/gate", fail)
	_, g6 := call(t, h, "POST", "/v1/gate", fail) // opus is tier 6 > projects max_tier 5
	if g6["action"] != "ask_human" || g6["approval_id"] == "" {
		t.Fatalf("beyond max_tier -> ask_human + approval: %v", g6)
	}
	code, _ := call(t, h, "POST", "/v1/approvals/"+g6["approval_id"].(string), `{"approve":true,"by":"leonardo"}`)
	if _, out := call(t, h, "POST", "/v1/models/resolve", `{"session_id":"s6"}`); code != 200 || out["model"] != "tier6-opus" {
		t.Fatalf("approval must unlock opus: %d %v", code, out)
	}
	_, done := call(t, h, "POST", "/v1/gate", `{"session_id":"s6","evidence":{"tests":{"exit_code":0}}}`)
	if done["action"] != "done" {
		t.Fatalf("green tests -> done: %v", done)
	}
	code, fin := call(t, h, "POST", "/v1/runs/s6/finish", `{"status":"succeeded"}`)
	if code != 200 || fin["status"] != "succeeded" {
		t.Fatalf("finish %d %v", code, fin)
	}
}

func TestGateWithoutEvidenceAsksCascade(t *testing.T) {
	h := policyServer(t, newMem(), classifier{nextStep: "escalate"})
	call(t, h, "POST", "/v1/route", `{"session_id":"s7","text":"x","task_type":"conversation","complexity":"simple"}`)
	_, g := call(t, h, "POST", "/v1/gate", `{"session_id":"s7","evidence":{"validation":"none"}}`)
	if g["action"] != "escalate" || g["decided_by"] != "local" {
		t.Fatalf("cascade decision must apply: %v", g)
	}
	h2 := policyServer(t, newMem(), classifier{nextStep: ""}) // cascade unsure -> default
	call(t, h2, "POST", "/v1/route", `{"session_id":"s8","text":"x","task_type":"conversation","complexity":"simple"}`)
	_, g2 := call(t, h2, "POST", "/v1/gate", `{"session_id":"s8","evidence":{"validation":"none"}}`)
	if g2["action"] != "done" || g2["decided_by"] != "default" {
		t.Fatalf("unsure cascade keeps default: %v", g2)
	}
}

func TestPolicyEndpointsNeedAuthAndLedger(t *testing.T) {
	h := policyServer(t, newMem(), nil)
	req := httptest.NewRequest("POST", "/v1/route", strings.NewReader(`{}`))
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != 401 {
		t.Fatalf("no auth: %d", rec.Code)
	}
	bare := (&Server{APIKey: "k", Log: slog.New(slog.NewTextHandler(io.Discard, nil))}).Handler()
	if code, _ := call(t, bare, "POST", "/v1/route", `{"session_id":"x"}`); code != 503 {
		t.Fatalf("no ledger: %d", code)
	}
}
