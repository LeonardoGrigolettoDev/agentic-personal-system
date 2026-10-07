package decide

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"strings"
	"sync"
)

// RequestHash is sha256 over the canonical JSON of state + questions. encoding/json sorts
// map keys, so semantically equal requests hash the same regardless of key order.
func RequestHash(r Request) string {
	b, _ := json.Marshal(struct {
		State     any        `json:"state"`
		Questions []Question `json:"questions"`
	}{r.State, r.Questions})
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

// CacheKey extends RequestHash with everything else that changes the outcome.
func CacheKey(r Request, threshold float64) string {
	h := sha256.New()
	b, _ := json.Marshal(struct {
		Hash      string   `json:"h"`
		Threshold float64  `json:"t"`
		Backends  []string `json:"b"`
		Images    []string `json:"i"`
	}{RequestHash(r), threshold, r.Backends, r.Images})
	h.Write(b)
	return "decision:v1:" + hex.EncodeToString(h.Sum(nil))
}

// StateText renders state for prompts: strings verbatim, anything else as indented JSON.
func StateText(state any) string {
	if s, ok := state.(string); ok {
		return s
	}
	b, err := json.MarshalIndent(state, "", "  ")
	if err != nil {
		return ""
	}
	return string(b)
}

// Prompt is the question text for prompt-based backends: its instructions, or its name.
func (q Question) Prompt() string {
	if q.Instructions != "" {
		return q.Instructions
	}
	return strings.ReplaceAll(q.Name, "_", " ") + "?"
}

// Usage accumulates billable usage across the engines that serve one request.
type Usage struct {
	mu          sync.Mutex
	InputTokens int
	CostUSD     float64
}

type usageKey struct{}

func WithUsage(ctx context.Context) (context.Context, *Usage) {
	u := &Usage{}
	return context.WithValue(ctx, usageKey{}, u), u
}

// AddUsage records usage if the context carries an accumulator; otherwise it is a no-op.
func AddUsage(ctx context.Context, inputTokens int, costUSD float64) {
	if u, ok := ctx.Value(usageKey{}).(*Usage); ok {
		u.mu.Lock()
		u.InputTokens += inputTokens
		u.CostUSD += costUSD
		u.mu.Unlock()
	}
}

func (u *Usage) Snapshot() (int, float64) {
	u.mu.Lock()
	defer u.mu.Unlock()
	return u.InputTokens, u.CostUSD
}
