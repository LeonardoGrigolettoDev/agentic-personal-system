package decide

import (
	"context"
	"log/slog"
	"slices"
	"time"
)

// Step records what one backend did for one request.
type Step struct {
	Backend   string   `json:"backend"`
	Asked     []string `json:"asked"`
	Accepted  []string `json:"accepted,omitempty"`
	LatencyMS int64    `json:"latency_ms"`
	Error     string   `json:"error,omitempty"`
}

// Cascade walks backends cheapest-first, forwarding only unresolved questions.
// An answer is accepted when it is not a refusal and Confidence >= threshold.
// Whatever is still unresolved after the last backend comes back as the best
// answer seen (or an empty one) with NeedsHuman=true.
type Cascade struct {
	engines   map[string]DecisionEngine
	order     []string
	threshold float64
	log       *slog.Logger
}

func NewCascade(engines []DecisionEngine, order []string, threshold float64, log *slog.Logger) *Cascade {
	m := make(map[string]DecisionEngine, len(engines))
	for _, e := range engines {
		m[e.Name()] = e
	}
	var active []string
	for _, name := range dedupe(order) {
		if _, ok := m[name]; ok {
			active = append(active, name)
		} else {
			log.Warn("backend in DECISION_BACKENDS is not registered (missing key/config?)", "backend", name)
		}
	}
	return &Cascade{engines: m, order: active, threshold: threshold, log: log}
}

func (c *Cascade) Name() string { return "cascade" }

// Order is the effective default backend order.
func (c *Cascade) Order() []string { return slices.Clone(c.order) }

// Has reports whether a backend is registered.
func (c *Cascade) Has(name string) bool { _, ok := c.engines[name]; return ok }

func (c *Cascade) Threshold(req Request) float64 {
	if req.Threshold > 0 {
		return req.Threshold
	}
	return c.threshold
}

func (c *Cascade) Decide(ctx context.Context, req Request) ([]Answer, error) {
	answers, _, err := c.DecideTrace(ctx, req)
	return answers, err
}

// Plan returns the backends this request will walk, in order.
func (c *Cascade) Plan(req Request) ([]string, error) {
	order := c.order
	if len(req.Backends) > 0 {
		for _, b := range req.Backends {
			if !c.Has(b) {
				return nil, invalid("backend %q is not available", b)
			}
		}
		order = req.Backends
	}
	if len(req.Images) > 0 && c.Has("openai") {
		order = append([]string{"openai"}, slices.DeleteFunc(slices.Clone(order), func(s string) bool { return s == "openai" })...)
	}
	return dedupe(order), nil
}

func dedupe(in []string) []string {
	var out []string
	for _, s := range in {
		if !slices.Contains(out, s) {
			out = append(out, s)
		}
	}
	return out
}

func (c *Cascade) DecideTrace(ctx context.Context, req Request) ([]Answer, []Step, error) {
	order, err := c.Plan(req)
	if err != nil {
		return nil, nil, err
	}
	threshold := c.Threshold(req)
	accepted := map[string]Answer{}
	best := map[string]Answer{}
	var trace []Step

	for _, name := range order {
		pending := unresolved(req.Questions, accepted)
		if len(pending) == 0 {
			break
		}
		if ctx.Err() != nil {
			break
		}
		sub := req
		sub.Questions = pending
		step := Step{Backend: name, Asked: names(pending)}
		start := time.Now()
		got, err := c.engines[name].Decide(ctx, sub)
		step.LatencyMS = time.Since(start).Milliseconds()
		if err != nil {
			step.Error = err.Error()
			c.log.WarnContext(ctx, "decision backend failed", "backend", name, "err", err)
			trace = append(trace, step)
			continue
		}
		asked := map[string]Question{}
		for _, q := range pending {
			asked[q.Name] = q
		}
		for _, a := range got {
			q, ok := asked[a.Name]
			if !ok || a.Type != q.Type {
				continue // ignore answers to questions we did not ask
			}
			a.Backend = name
			if !a.Refused && a.Confidence >= threshold {
				accepted[a.Name] = a
				step.Accepted = append(step.Accepted, a.Name)
			} else if prev, ok := best[a.Name]; !ok || better(a, prev) {
				best[a.Name] = a
			}
		}
		trace = append(trace, step)
	}

	out := make([]Answer, 0, len(req.Questions))
	for _, q := range req.Questions {
		if a, ok := accepted[q.Name]; ok {
			out = append(out, a)
			continue
		}
		a, ok := best[q.Name]
		if !ok {
			a = Answer{Name: q.Name, Type: q.Type, Backend: "none"}
		}
		a.NeedsHuman = true
		out = append(out, a)
	}
	return out, trace, nil
}

// better prefers non-refusals, then higher confidence.
func better(a, b Answer) bool {
	if a.Refused != b.Refused {
		return !a.Refused
	}
	return a.Confidence > b.Confidence
}

func unresolved(qs []Question, done map[string]Answer) []Question {
	var out []Question
	for _, q := range qs {
		if _, ok := done[q.Name]; !ok {
			out = append(out, q)
		}
	}
	return out
}

func names(qs []Question) []string {
	out := make([]string, len(qs))
	for i, q := range qs {
		out[i] = q.Name
	}
	return out
}

// Summary describes a finished set of answers for logging/persistence.
func Summary(answers []Answer) (backend string, minConfidence float64, needsHuman bool) {
	minConfidence = 1
	for i, a := range answers {
		minConfidence = min(minConfidence, a.Confidence)
		needsHuman = needsHuman || a.NeedsHuman
		switch {
		case i == 0:
			backend = a.Backend
		case backend != a.Backend:
			backend = "cascade"
		}
	}
	if len(answers) == 0 {
		return "none", 0, true
	}
	return backend, minConfidence, needsHuman
}

var _ DecisionEngine = (*Cascade)(nil)
