package policy

import (
	"fmt"
	"math"
	"time"
)

// ---------------------------------------------------------------- budget (§15)

const (
	BudgetOK        = "ok"
	BudgetWarn      = "warn"
	BudgetExhausted = "exhausted"
)

type Usage struct {
	CostUSD    float64
	Tokens     int
	Iterations int
}

type Limits struct {
	MaxCostUSD    float64
	MaxTokens     int
	MaxIterations int
	Deadline      *time.Time
}

// Spend is global spend against a configured limit (budgets table: day / month).
type Spend struct {
	Scope    string // e.g. "global/day"
	SpentUSD float64
	LimitUSD float64
}

type Remaining struct {
	USD        float64 `json:"usd"`
	Tokens     int     `json:"tokens"`
	Iterations int     `json:"iterations"`
}

type Budget struct {
	State     string    `json:"state"`
	Reasons   []string  `json:"reasons,omitempty"`
	Remaining Remaining `json:"remaining"`
}

// CheckBudget evaluates every limit; the worst state wins. Zero limits mean "unlimited".
func (p *Policy) CheckBudget(u Usage, l Limits, global []Spend, now time.Time) Budget {
	b := Budget{State: BudgetOK, Remaining: Remaining{USD: math.Inf(1), Tokens: math.MaxInt, Iterations: math.MaxInt}}
	raise := func(state, reason string) {
		if state == BudgetExhausted || b.State == BudgetOK {
			b.State = state
		}
		b.Reasons = append(b.Reasons, reason)
	}
	ratio := func(used, limit float64, name string) {
		switch {
		case limit <= 0:
		case used >= limit:
			raise(BudgetExhausted, fmt.Sprintf("%s %.4g/%.4g", name, used, limit))
		case used >= limit*p.BudgetWarnRatio:
			raise(BudgetWarn, fmt.Sprintf("%s at %.0f%% (%.4g/%.4g)", name, 100*used/limit, used, limit))
		}
	}
	ratio(u.CostUSD, l.MaxCostUSD, "run cost USD")
	ratio(float64(u.Tokens), float64(l.MaxTokens), "run tokens")
	ratio(float64(u.Iterations), float64(l.MaxIterations), "run iterations")
	if l.MaxCostUSD > 0 {
		b.Remaining.USD = max(l.MaxCostUSD-u.CostUSD, 0)
	}
	if l.MaxTokens > 0 {
		b.Remaining.Tokens = max(l.MaxTokens-u.Tokens, 0)
	}
	if l.MaxIterations > 0 {
		b.Remaining.Iterations = max(l.MaxIterations-u.Iterations, 0)
	}
	if l.Deadline != nil && !now.Before(*l.Deadline) {
		raise(BudgetExhausted, "deadline passed")
	}
	for _, s := range global {
		ratio(s.SpentUSD, s.LimitUSD, s.Scope+" USD")
		if s.LimitUSD > 0 {
			b.Remaining.USD = min(b.Remaining.USD, max(s.LimitUSD-s.SpentUSD, 0))
		}
	}
	if math.IsInf(b.Remaining.USD, 1) {
		b.Remaining.USD = -1 // unlimited
	}
	if b.Remaining.Tokens == math.MaxInt {
		b.Remaining.Tokens = -1
	}
	if b.Remaining.Iterations == math.MaxInt {
		b.Remaining.Iterations = -1
	}
	return b
}

// ---------------------------------------------------------------- gate (§7 escalation, §2 decision gate)

const (
	ActionDone     = "done"
	ActionRepair   = "repair"
	ActionEscalate = "escalate"
	ActionFail     = "fail"
	ActionAskHuman = "ask_human"
)

type TestResult struct {
	ExitCode   int    `json:"exit_code"`
	OutputTail string `json:"output_tail,omitempty"`
	Command    string `json:"command,omitempty"`
}

type Evidence struct {
	Validation          string      `json:"validation"` // pass | fail | none
	Failures            []string    `json:"failures,omitempty"`
	Tests               *TestResult `json:"tests,omitempty"`
	Confidence          *float64    `json:"confidence,omitempty"`
	ArchitecturalChange bool        `json:"architectural_change,omitempty"`
	Critical            bool        `json:"critical,omitempty"`
}

// RunState is what the gate needs to know about a run.
type RunState struct {
	Model               string
	ConsecutiveFailures int // before this gate
	Iterations          int // gate cycles so far, before this gate
	MaxIterations       int
	MaxTier             int
	Budget              Budget
}

type Gate struct {
	Action    string   `json:"action"`
	NextModel string   `json:"next_model,omitempty"`
	Tier      int      `json:"tier,omitempty"`
	Reasons   []string `json:"reasons"`
	// Ask is set when no deterministic rule applies: the caller must ask the decision cascade
	// to choose among these actions (then call Apply).
	Ask []string `json:"-"`
}

// Decide applies the deterministic guards in order. When none applies it returns Ask with the
// candidate actions for the typed decision (rules -> local -> Jev).
func (p *Policy) Decide(r RunState, ev Evidence) Gate {
	failed := ev.Validation == "fail" || (ev.Tests != nil && ev.Tests.ExitCode != 0)
	passed := ev.Validation == "pass" || (ev.Tests != nil && ev.Tests.ExitCode == 0 && ev.Validation != "fail")
	failures := r.ConsecutiveFailures
	if failed {
		failures++
	}

	switch {
	case r.Budget.State == BudgetExhausted:
		return Gate{Action: ActionFail, Reasons: append([]string{"budget exhausted"}, r.Budget.Reasons...)}
	case passed:
		return Gate{Action: ActionDone, Reasons: []string{"validation passed"}}
	case r.MaxIterations > 0 && r.Iterations+1 >= r.MaxIterations && failed:
		return Gate{Action: ActionFail, Reasons: []string{fmt.Sprintf("max_iterations %d reached", r.MaxIterations)}}
	case failed && failures >= p.EscalateAfterFailures:
		return p.Escalate(r, fmt.Sprintf("%d consecutive failures on %s", failures, r.Model))
	case ev.Critical && p.Tier(r.Model) < 5:
		return p.Escalate(r, "critical task below frontier tier")
	case ev.ArchitecturalChange && p.Tier(r.Model) < 4:
		return p.Escalate(r, "large architectural change below strong tier")
	case ev.Confidence != nil && *ev.Confidence < p.LowConfidence:
		return p.Escalate(r, fmt.Sprintf("low confidence %.2f", *ev.Confidence))
	case failed:
		return Gate{Action: ActionRepair, Ask: []string{ActionRepair, ActionEscalate},
			Reasons: []string{fmt.Sprintf("validation failed (%d/%d before escalation)", failures, p.EscalateAfterFailures)}}
	default:
		return Gate{Action: ActionDone, Ask: []string{ActionDone, ActionRepair, ActionEscalate},
			Reasons: []string{"no validation evidence"}}
	}
}

// Escalate moves one step up the ladder, or asks a human when the agent's max_tier or the ladder ends.
func (p *Policy) Escalate(r RunState, reason string) Gate {
	next, ok := p.Next(r.Model)
	if !ok {
		return Gate{Action: ActionAskHuman, Reasons: []string{reason, "already at the top of the ladder"}}
	}
	if (r.MaxTier > 0 && p.Tier(next) > r.MaxTier) || p.Models[next].RequiresApproval {
		return Gate{Action: ActionAskHuman, NextModel: next, Tier: p.Tier(next),
			Reasons: []string{reason, fmt.Sprintf("%s (tier %d) needs approval", next, p.Tier(next))}}
	}
	return Gate{Action: ActionEscalate, NextModel: next, Tier: p.Tier(next), Reasons: []string{reason}}
}

// Apply turns the cascade's choice into a gate result (unknown choices keep the default action).
func (p *Policy) Apply(r RunState, g Gate, choice string, why string) Gate {
	switch choice {
	case ActionEscalate:
		e := p.Escalate(r, why)
		e.Reasons = append(g.Reasons, e.Reasons...)
		return e
	case ActionRepair, ActionDone:
		return Gate{Action: choice, Reasons: append(g.Reasons, why)}
	}
	return Gate{Action: g.Action, Reasons: append(g.Reasons, "decision unavailable: default "+g.Action)}
}
