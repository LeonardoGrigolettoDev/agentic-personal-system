package policy

import (
	"fmt"
	"slices"
)

// Stat is the learned performance of one starting model on one task type
// (runs that finished; "solved" = succeeded without escalating).
type Stat struct {
	Model          string
	Runs           int
	Solved         int
	CostPerSuccess float64 // USD, 0 when nothing solved
}

func (s Stat) SuccessRate() float64 {
	if s.Runs == 0 {
		return 0
	}
	return float64(s.Solved) / float64(s.Runs)
}

// Choice is where a run starts.
type Choice struct {
	Model  string `json:"model"`
	Tier   int    `json:"tier"`
	Reason string `json:"reason"`
}

// StartModel picks the starting model: policy (task type x complexity), then learned stats, capped by
// the agent's max_tier. Learned stats may move the start to a model with enough samples that meets the
// target success rate at a lower cost per success (cheaper first), or away from a model that keeps failing.
func (p *Policy) StartModel(agent Agent, taskType, complexity string, stats []Stat) Choice {
	model, reason := p.DefaultModel, "default_model"
	if tt, ok := p.TaskTypes[taskType]; ok {
		if m, ok := tt.TierByComplexity[complexity]; ok {
			model, reason = m, fmt.Sprintf("policy %s/%s", taskType, complexity)
		}
	} else if agent.DefaultModel != "" {
		model, reason = agent.DefaultModel, "agent default_model"
	}

	if learned, why, ok := p.learned(model, stats); ok {
		model, reason = learned, why
	}
	if capped, ok := p.capTier(model, agent.MaxTier); ok {
		model, reason = capped, reason+fmt.Sprintf("; capped to agent max_tier %d", agent.MaxTier)
	}
	return Choice{Model: model, Tier: p.Tier(model), Reason: reason}
}

func (p *Policy) learned(policyModel string, stats []Stat) (string, string, bool) {
	eligible := func(s Stat) bool {
		return s.Runs >= p.Learning.MinSamples && s.SuccessRate() >= p.Learning.TargetSuccessRate && s.Solved > 0
	}
	var best *Stat
	for i := range stats {
		s := &stats[i]
		if _, known := p.Models[s.Model]; !known || !eligible(*s) {
			continue
		}
		if best == nil || s.CostPerSuccess < best.CostPerSuccess {
			best = s
		}
	}
	if best == nil || best.Model == policyModel {
		return "", "", false
	}
	// Only move when the policy model is either unproven or more expensive per success.
	for _, s := range stats {
		if s.Model == policyModel && eligible(s) && s.CostPerSuccess <= best.CostPerSuccess {
			return "", "", false
		}
	}
	return best.Model, fmt.Sprintf("learned: %s solves %.0f%% at $%.4f/success (n=%d)",
		best.Model, 100*best.SuccessRate(), best.CostPerSuccess, best.Runs), true
}

// capTier lowers a model to the highest ladder model within maxTier.
func (p *Policy) capTier(model string, maxTier int) (string, bool) {
	if maxTier <= 0 || p.Tier(model) <= maxTier {
		return model, false
	}
	for i := len(p.Ladder) - 1; i >= 0; i-- {
		if p.Tier(p.Ladder[i]) <= maxTier {
			return p.Ladder[i], true
		}
	}
	return p.Ladder[0], true
}

// Next is the next ladder step above model (a model off the ladder escalates from its tier).
func (p *Policy) Next(model string) (string, bool) {
	if i := slices.Index(p.Ladder, model); i >= 0 {
		if i+1 < len(p.Ladder) {
			return p.Ladder[i+1], true
		}
		return "", false
	}
	tier := p.Tier(model)
	for _, m := range p.Ladder {
		if p.Tier(m) > tier {
			return m, true
		}
	}
	return "", false
}

// Resolve applies the hard per-call rules to the model a run is on: unknown aliases fall back to
// the default, and approval-gated models are lowered to the best model below unless approved.
func (p *Policy) Resolve(model string, approved bool) (string, string) {
	m, ok := p.Models[model]
	if !ok {
		return p.DefaultModel, fmt.Sprintf("unknown model %q -> default", model)
	}
	if m.RequiresApproval && !approved {
		capped, _ := p.capTier(model, m.Tier-1)
		return capped, fmt.Sprintf("%s requires approval -> %s", model, capped)
	}
	return model, ""
}
