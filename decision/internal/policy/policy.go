// Package policy holds the routing, budget and escalation rules (config/routing.yaml +
// agents/*/agent.yaml). Everything here is pure and deterministic; persistence lives in ledger.
package policy

import (
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"sort"

	"go.yaml.in/yaml/v3"
)

// Complexities, lowest first (the score levels the classifier answers with).
var Complexities = []string{"trivial", "simple", "medium", "hard", "critical"}

// Domains are the agent domains (migrations/001 'domain' enum).
var Domains = []string{"chief", "engineering", "finance", "projects", "personal", "learning"}

// Tenants mirror the 'tenant' enum.
var Tenants = []string{"nitro", "pessoal", "shared"}

type Price struct {
	Input     float64 `yaml:"input"`      // USD per 1M input tokens
	Output    float64 `yaml:"output"`     // USD per 1M output tokens
	CacheRead float64 `yaml:"cache_read"` // USD per 1M cached input tokens (0 = billed as input)
}

type Model struct {
	Tier             int   `yaml:"tier"`
	Price            Price `yaml:"price"`
	RequiresApproval bool  `yaml:"requires_approval"`
}

type TaskType struct {
	Description      string            `yaml:"description"`
	Validator        string            `yaml:"validator"` // none | command | review | schema
	TierByComplexity map[string]string `yaml:"tier_by_complexity"`
	Skills           []string          `yaml:"skills"`
}

type Learning struct {
	MinSamples        int     `yaml:"min_samples"`
	TargetSuccessRate float64 `yaml:"target_success_rate"`
}

type Routing struct {
	DefaultModel          string              `yaml:"default_model"`
	EscalateAfterFailures int                 `yaml:"escalate_after_failures"`
	LowConfidence         float64             `yaml:"low_confidence"`
	BudgetWarnRatio       float64             `yaml:"budget_warn_ratio"`
	Learning              Learning            `yaml:"learning"`
	Ladder                []string            `yaml:"ladder"`
	Models                map[string]Model    `yaml:"models"`
	TaskTypes             map[string]TaskType `yaml:"task_types"`
	DomainAgent           map[string]string   `yaml:"domain_agent"`
	DomainDefaultTaskType map[string]string   `yaml:"domain_default_task_type"`
}

type TokenBudget struct {
	Total     int `yaml:"total" json:"total"`
	Decision  int `yaml:"decision" json:"decision"`
	Retrieval int `yaml:"retrieval" json:"retrieval"`
	Execution int `yaml:"execution" json:"execution"`
	Final     int `yaml:"final" json:"final"`
}

// Agent is agents/<slug>/agent.yaml (docs/ARCHITECTURE.md §12).
type Agent struct {
	Slug           string      `yaml:"agent" json:"agent"`
	Name           string      `yaml:"name" json:"name"`
	Domain         string      `yaml:"domain" json:"domain"`
	Description    string      `yaml:"description" json:"description"`
	DefaultModel   string      `yaml:"default_model" json:"default_model"`
	MaxTier        int         `yaml:"max_tier" json:"max_tier"`
	AllowedTenants []string    `yaml:"allowed_tenants" json:"allowed_tenants"`
	AllowedDomains []string    `yaml:"allowed_domains" json:"allowed_domains"`
	DenyDomains    []string    `yaml:"deny_domains" json:"deny_domains"`
	AllowedTools   []string    `yaml:"allowed_tools" json:"allowed_tools"`
	MaxCostPerRun  float64     `yaml:"max_cost_per_run" json:"max_cost_per_run"`
	TokenBudget    TokenBudget `yaml:"token_budget" json:"token_budget"`
	MaxIterations  int         `yaml:"max_iterations" json:"max_iterations"`
}

type Policy struct {
	Routing
	Agents map[string]Agent
}

// Load reads and validates routing.yaml and every agents/*/agent.yaml.
func Load(routingPath, agentsDir string) (*Policy, error) {
	b, err := os.ReadFile(routingPath)
	if err != nil {
		return nil, err
	}
	p := &Policy{Agents: map[string]Agent{}}
	if err := yaml.Unmarshal(b, &p.Routing); err != nil {
		return nil, fmt.Errorf("%s: %w", routingPath, err)
	}
	files, err := filepath.Glob(filepath.Join(agentsDir, "*", "agent.yaml"))
	if err != nil {
		return nil, err
	}
	for _, f := range files {
		b, err := os.ReadFile(f)
		if err != nil {
			return nil, err
		}
		var a Agent
		if err := yaml.Unmarshal(b, &a); err != nil {
			return nil, fmt.Errorf("%s: %w", f, err)
		}
		p.Agents[a.Slug] = a
	}
	return p, p.Validate()
}

// Validate checks internal consistency so a typo fails at startup, not mid-task.
func (p *Policy) Validate() error {
	p.defaults()
	if _, ok := p.Models[p.DefaultModel]; !ok {
		return fmt.Errorf("default_model %q is not in models", p.DefaultModel)
	}
	if len(p.Ladder) == 0 {
		return fmt.Errorf("ladder is empty")
	}
	prevTier := 0
	for _, m := range p.Ladder {
		model, ok := p.Models[m]
		if !ok {
			return fmt.Errorf("ladder model %q is not in models", m)
		}
		if model.Tier < prevTier {
			return fmt.Errorf("ladder must be ordered by tier (%q is tier %d after tier %d)", m, model.Tier, prevTier)
		}
		prevTier = model.Tier
	}
	for name, tt := range p.TaskTypes {
		if !slices.Contains([]string{"none", "command", "review", "schema"}, tt.Validator) {
			return fmt.Errorf("task_types.%s.validator %q must be none|command|review|schema", name, tt.Validator)
		}
		for _, c := range Complexities {
			m, ok := tt.TierByComplexity[c]
			if !ok {
				return fmt.Errorf("task_types.%s.tier_by_complexity is missing %q", name, c)
			}
			if _, ok := p.Models[m]; !ok {
				return fmt.Errorf("task_types.%s.tier_by_complexity.%s: unknown model %q", name, c, m)
			}
		}
	}
	for d, tt := range p.DomainDefaultTaskType {
		if _, ok := p.TaskTypes[tt]; !ok {
			return fmt.Errorf("domain_default_task_type.%s: unknown task type %q", d, tt)
		}
	}
	for _, d := range Domains {
		if _, ok := p.DomainAgent[d]; !ok {
			return fmt.Errorf("domain_agent is missing %q", d)
		}
	}
	for slug, a := range p.Agents {
		if !slices.Contains(Domains, a.Domain) {
			return fmt.Errorf("agent %s: unknown domain %q", slug, a.Domain)
		}
		if _, ok := p.Models[a.DefaultModel]; !ok {
			return fmt.Errorf("agent %s: unknown default_model %q", slug, a.DefaultModel)
		}
		if a.MaxTier < 1 || a.MaxTier > 7 {
			return fmt.Errorf("agent %s: max_tier must be 1..7", slug)
		}
	}
	return nil
}

func (p *Policy) defaults() {
	if p.EscalateAfterFailures <= 0 {
		p.EscalateAfterFailures = 2
	}
	if p.LowConfidence <= 0 {
		p.LowConfidence = 0.5
	}
	if p.BudgetWarnRatio <= 0 || p.BudgetWarnRatio >= 1 {
		p.BudgetWarnRatio = 0.8
	}
	if p.Learning.MinSamples <= 0 {
		p.Learning.MinSamples = 10
	}
	if p.Learning.TargetSuccessRate <= 0 {
		p.Learning.TargetSuccessRate = 0.8
	}
}

// Agent returns the agent for a slug, falling back to chief, then to a conservative default.
func (p *Policy) Agent(slug string) Agent {
	if a, ok := p.Agents[slug]; ok {
		return a
	}
	if a, ok := p.Agents["chief"]; ok {
		return a
	}
	return Agent{Slug: "chief", Domain: "chief", DefaultModel: p.DefaultModel, MaxTier: 5, MaxCostPerRun: 0.5,
		TokenBudget: TokenBudget{Total: 30000}, MaxIterations: 8}
}

// TaskTypeNames returns the configured task types, sorted (stable classifier options).
func (p *Policy) TaskTypeNames() []string {
	names := make([]string, 0, len(p.TaskTypes))
	for n := range p.TaskTypes {
		names = append(names, n)
	}
	sort.Strings(names)
	return names
}

// Tier of a model alias (0 when unknown).
func (p *Policy) Tier(model string) int { return p.Models[model].Tier }

// Cost in USD of one call. Cached input is billed at cache_read when priced, else as input.
func (p *Policy) Cost(model string, input, output, cacheRead int) float64 {
	pr := p.Models[model].Price
	cached := pr.CacheRead
	if cached == 0 {
		cached = pr.Input
	}
	fresh := max(input-cacheRead, 0)
	return (float64(fresh)*pr.Input + float64(cacheRead)*cached + float64(output)*pr.Output) / 1e6
}
