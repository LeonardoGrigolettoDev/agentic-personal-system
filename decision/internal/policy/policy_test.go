package policy

import (
	"math"
	"strings"
	"testing"
	"time"
)

// The repo's real policy files: a broken routing.yaml or agent.yaml fails here, not at runtime.
func load(t *testing.T) *Policy {
	t.Helper()
	p, err := Load("../../../config/routing.yaml", "../../../agents")
	if err != nil {
		t.Fatalf("load real policy: %v", err)
	}
	return p
}

func TestRealPolicyLoads(t *testing.T) {
	p := load(t)
	if len(p.Agents) != 6 {
		t.Fatalf("want 6 agents, got %d", len(p.Agents))
	}
	for _, d := range Domains {
		if _, ok := p.Agents[p.DomainAgent[d]]; !ok {
			t.Errorf("domain %s maps to missing agent %q", d, p.DomainAgent[d])
		}
	}
}

func TestValidateCatchesTypos(t *testing.T) {
	p := load(t)
	tt := p.TaskTypes["debugging"]
	tt.TierByComplexity = map[string]string{"trivial": "tier3-code"}
	p.TaskTypes["debugging"] = tt
	if err := p.Validate(); err == nil || !strings.Contains(err.Error(), "missing") {
		t.Fatalf("expected missing complexity error, got %v", err)
	}
}

func TestStartModelPolicyAndCap(t *testing.T) {
	p := load(t)
	eng := p.Agent("engineering")
	if c := p.StartModel(eng, "debugging", "medium", nil); c.Model != "tier3-code" || c.Tier != 3 {
		t.Fatalf("policy start: %+v", c)
	}
	personal := p.Agent("personal") // max_tier 4
	if c := p.StartModel(personal, "architecture", "critical", nil); p.Tier(c.Model) > 4 || !strings.Contains(c.Reason, "capped") {
		t.Fatalf("cap to max_tier: %+v", c)
	}
	if c := p.StartModel(eng, "nonexistent", "medium", nil); c.Model != eng.DefaultModel {
		t.Fatalf("unknown task type should use agent default: %+v", c)
	}
}

func TestStartModelLearned(t *testing.T) {
	p := load(t)
	eng := p.Agent("engineering")
	// §14 example: K2.7 4 attempts ($1.00) vs Sonnet 1 attempt ($0.80) -> Sonnet is cheaper per success
	stats := []Stat{
		{Model: "tier3-code", Runs: 20, Solved: 10, CostPerSuccess: 1.00},   // 50%: below target
		{Model: "tier5-sonnet", Runs: 12, Solved: 11, CostPerSuccess: 0.80}, // 92%
	}
	if c := p.StartModel(eng, "debugging", "medium", stats); c.Model != "tier5-sonnet" || !strings.HasPrefix(c.Reason, "learned") {
		t.Fatalf("learned routing: %+v", c)
	}
	few := []Stat{{Model: "tier5-sonnet", Runs: 3, Solved: 3, CostPerSuccess: 0.1}}
	if c := p.StartModel(eng, "debugging", "medium", few); c.Model != "tier3-code" {
		t.Fatalf("below min_samples must keep policy: %+v", c)
	}
	good := []Stat{
		{Model: "tier3-code", Runs: 30, Solved: 28, CostPerSuccess: 0.30},
		{Model: "tier5-sonnet", Runs: 30, Solved: 30, CostPerSuccess: 0.90},
	}
	if c := p.StartModel(eng, "debugging", "medium", good); c.Model != "tier3-code" {
		t.Fatalf("proven cheaper policy model must stay: %+v", c)
	}
}

func TestCost(t *testing.T) {
	p := load(t)
	// 1M fresh input + 1M output on Sonnet = $2 + $10
	if c := p.Cost("tier5-sonnet", 1_000_000, 1_000_000, 0); math.Abs(c-12) > 1e-9 {
		t.Fatalf("cost %f", c)
	}
	// cached input billed at cache_read
	if c := p.Cost("tier3-code", 1_000_000, 0, 1_000_000); math.Abs(c-0.19) > 1e-9 {
		t.Fatalf("cached cost %f", c)
	}
	if c := p.Cost("local-qwen", 5000, 5000, 0); c != 0 {
		t.Fatalf("local must be free: %f", c)
	}
}

func TestResolveApproval(t *testing.T) {
	p := load(t)
	if m, why := p.Resolve("tier7-fable", false); m != "tier6-opus" || why == "" {
		t.Fatalf("unapproved fable: %s %s", m, why)
	}
	if m, _ := p.Resolve("tier7-fable", true); m != "tier7-fable" {
		t.Fatalf("approved fable: %s", m)
	}
	if m, _ := p.Resolve("gpt-4o", false); m != p.DefaultModel {
		t.Fatalf("unknown alias: %s", m)
	}
}

func TestBudget(t *testing.T) {
	p := load(t)
	l := Limits{MaxCostUSD: 1, MaxTokens: 10000, MaxIterations: 8}
	if b := p.CheckBudget(Usage{CostUSD: 0.1, Tokens: 100}, l, nil, time.Now()); b.State != BudgetOK {
		t.Fatalf("ok: %+v", b)
	}
	if b := p.CheckBudget(Usage{CostUSD: 0.85}, l, nil, time.Now()); b.State != BudgetWarn {
		t.Fatalf("warn: %+v", b)
	}
	b := p.CheckBudget(Usage{CostUSD: 0.2}, l, []Spend{{Scope: "global/day", SpentUSD: 5, LimitUSD: 5}}, time.Now())
	if b.State != BudgetExhausted || b.Remaining.USD != 0 {
		t.Fatalf("global exhausted: %+v", b)
	}
	past := time.Now().Add(-time.Minute)
	if b := p.CheckBudget(Usage{}, Limits{Deadline: &past}, nil, time.Now()); b.State != BudgetExhausted {
		t.Fatalf("deadline: %+v", b)
	}
	if b := p.CheckBudget(Usage{}, Limits{}, nil, time.Now()); b.Remaining.USD != -1 || b.State != BudgetOK {
		t.Fatalf("unlimited: %+v", b)
	}
}

func run(model string, failures int) RunState {
	return RunState{Model: model, ConsecutiveFailures: failures, MaxIterations: 8, MaxTier: 6, Budget: Budget{State: BudgetOK}}
}

func TestGateDeterministicGuards(t *testing.T) {
	p := load(t)
	fail := Evidence{Validation: "fail"}
	cases := []struct {
		name   string
		r      RunState
		ev     Evidence
		action string
		next   string
	}{
		{"pass", run("tier3-code", 1), Evidence{Validation: "pass"}, ActionDone, ""},
		{"tests green", run("tier3-code", 0), Evidence{Tests: &TestResult{ExitCode: 0}}, ActionDone, ""},
		{"first failure repairs", run("tier3-code", 0), fail, ActionRepair, ""},
		{"second failure escalates", run("tier3-code", 1), fail, ActionEscalate, "tier4-pro"},
		{"tests red count as failure", run("tier3-code", 1), Evidence{Tests: &TestResult{ExitCode: 1}}, ActionEscalate, "tier4-pro"},
		{"critical below frontier", run("tier3-code", 0), Evidence{Critical: true}, ActionEscalate, "tier4-pro"},
		{"budget exhausted", RunState{Model: "tier3-code", Budget: Budget{State: BudgetExhausted}}, fail, ActionFail, ""},
		{"max iterations", RunState{Model: "tier3-code", Iterations: 7, MaxIterations: 8, Budget: Budget{State: BudgetOK}}, fail, ActionFail, ""},
		{"max tier needs human", RunState{Model: "tier4-k3", ConsecutiveFailures: 1, MaxTier: 4, Budget: Budget{State: BudgetOK}}, fail, ActionAskHuman, "tier5-sonnet"},
		{"fable needs approval", RunState{Model: "tier6-opus", ConsecutiveFailures: 1, MaxTier: 7, Budget: Budget{State: BudgetOK}}, fail, ActionAskHuman, "tier7-fable"},
	}
	for _, c := range cases {
		g := p.Decide(c.r, c.ev)
		if g.Action != c.action || g.NextModel != c.next {
			t.Errorf("%s: got %s -> %q (%v), want %s -> %q", c.name, g.Action, g.NextModel, g.Reasons, c.action, c.next)
		}
	}
}

func TestGateAsksWhenAmbiguous(t *testing.T) {
	p := load(t)
	g := p.Decide(run("tier3-code", 0), Evidence{Validation: "none"})
	if len(g.Ask) == 0 {
		t.Fatalf("no evidence must defer to the decision cascade: %+v", g)
	}
	if a := p.Apply(run("tier3-code", 0), g, ActionEscalate, "jev: low quality"); a.Action != ActionEscalate || a.NextModel != "tier4-pro" {
		t.Fatalf("apply escalate: %+v", a)
	}
	if a := p.Apply(run("tier3-code", 0), g, "", ""); a.Action != g.Action {
		t.Fatalf("missing choice keeps default: %+v", a)
	}
}

func TestLadderNext(t *testing.T) {
	p := load(t)
	if n, ok := p.Next("tier2-cheap"); !ok || n != "tier2-flash" {
		t.Fatalf("next %s", n)
	}
	if _, ok := p.Next("tier7-fable"); ok {
		t.Fatal("fable is the top")
	}
	if n, _ := p.Next("local-qwen"); p.Tier(n) <= 2 {
		t.Fatalf("off-ladder model escalates to a higher tier, got %s", n)
	}
}
