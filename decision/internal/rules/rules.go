// Package rules is the deterministic Tier-0 backend: expr-lang expressions over the request
// state, loaded once from YAML. A matching rule answers with confidence 1.0.
package rules

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"slices"

	"aios/decision/internal/decide"
	"github.com/expr-lang/expr"
	"github.com/expr-lang/expr/vm"
	"go.yaml.in/yaml/v3"
)

type Rule struct {
	Name     string `yaml:"name"`
	Question string `yaml:"question"`
	When     string `yaml:"when"`
	Answer   string `yaml:"answer"`
	program  *vm.Program
}

type Engine struct {
	rules []Rule
	log   *slog.Logger
}

func Load(path string, log *slog.Logger) (*Engine, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	return Parse(b, log)
}

func Parse(src []byte, log *slog.Logger) (*Engine, error) {
	var file struct {
		Rules []Rule `yaml:"rules"`
	}
	if err := yaml.Unmarshal(src, &file); err != nil {
		return nil, fmt.Errorf("rules: %w", err)
	}
	env := map[string]any{"state": map[string]any{}}
	for i := range file.Rules {
		r := &file.Rules[i]
		if r.Name == "" || r.Question == "" || r.When == "" || r.Answer == "" {
			return nil, fmt.Errorf("rules[%d]: name, question, when and answer are required", i)
		}
		p, err := expr.Compile(r.When, expr.Env(env), expr.AsBool())
		if err != nil {
			return nil, fmt.Errorf("rule %q: %w", r.Name, err)
		}
		r.program = p
	}
	return &Engine{rules: file.Rules, log: log}, nil
}

func (e *Engine) Name() string { return "rules" }

func (e *Engine) Len() int { return len(e.rules) }

// Decide answers each question with the first matching rule (file order). A rule that fails at
// runtime (e.g. state has no "text", or text is not a string) simply does not match.
func (e *Engine) Decide(ctx context.Context, req decide.Request) ([]decide.Answer, error) {
	env := map[string]any{"state": normalizeState(req.State)}
	var out []decide.Answer
	for _, q := range req.Questions {
		for _, r := range e.rules {
			if r.Question != q.Name || !slices.Contains(q.Options(), r.Answer) {
				continue
			}
			res, err := expr.Run(r.program, env)
			if err != nil {
				e.log.DebugContext(ctx, "rule did not evaluate", "rule", r.Name, "err", err)
				continue
			}
			if res == true {
				out = append(out, decide.AnswerFrom(q, decide.OneHot(q, r.Answer), e.Name()))
				break
			}
		}
	}
	return out, nil
}

// normalizeState exposes a plain-string state as state.text so rules can always use it.
func normalizeState(s any) any {
	if str, ok := s.(string); ok {
		return map[string]any{"text": str}
	}
	return s
}
