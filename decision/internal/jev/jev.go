// Package jev maps decisions onto TypeSafe AI's "System One" API (model Jev).
// https://docs.typesafe.ai/api.md - questions are a map keyed by name; binary is "noul".
package jev

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strconv"
	"time"

	"aios/decision/internal/decide"
	"aios/decision/internal/httpx"
)

const (
	DefaultBaseURL = "https://api.typesafe.ai"
	// Jev allows 32k tokens for state + longest question. ~3 bytes/token keeps us under it.
	MaxStateBytes = 96 << 10
	// USD per input token ($0.042 / 1M); output is free.
	costPerInputToken = 0.042 / 1e6
)

type Engine struct {
	baseURL string
	model   string
	client  *httpx.Client
}

func New(baseURL, apiKey, model string, timeout time.Duration) *Engine {
	if baseURL == "" {
		baseURL = DefaultBaseURL
	}
	return &Engine{
		baseURL: baseURL,
		model:   model,
		client: &httpx.Client{
			HTTP:       &http.Client{Timeout: timeout},
			Token:      apiKey,
			MaxRetries: 3,
			Backoff:    250 * time.Millisecond,
			RetryOn:    []int{http.StatusTooManyRequests, 529},
		},
	}
}

// WithBackoff shortens retry delays (tests).
func (e *Engine) WithBackoff(d time.Duration) *Engine { e.client.Backoff = d; return e }

func (e *Engine) Name() string { return "jev" }

type question struct {
	Type         string `json:"type"`
	Instructions string `json:"instructions,omitempty"`
	Criteria     any    `json:"criteria,omitempty"`
}

type request struct {
	State     any                 `json:"state"`
	Model     string              `json:"model"`
	Questions map[string]question `json:"questions"`
}

type answer struct {
	Type          string             `json:"type"`
	Noul          *float64           `json:"noul"`
	Choice        string             `json:"choice"`
	Score         *float64           `json:"score"`
	Probabilities map[string]float64 `json:"probabilities"`
	Confidence    *float64           `json:"confidence"`
}

type response struct {
	Model   string            `json:"model"`
	Answers map[string]answer `json:"answers"`
	Usage   struct {
		InputTokens int `json:"input_tokens"`
	} `json:"usage"`
}

func (e *Engine) Decide(ctx context.Context, req decide.Request) ([]decide.Answer, error) {
	state, err := json.Marshal(req.State)
	if err != nil {
		return nil, err
	}
	if len(state) > MaxStateBytes {
		return nil, fmt.Errorf("jev: state is %d bytes, limit %d", len(state), MaxStateBytes)
	}
	body := request{State: req.State, Model: e.model, Questions: make(map[string]question, len(req.Questions))}
	for _, q := range req.Questions {
		body.Questions[q.Name] = toJev(q)
	}
	var resp response
	if err := e.client.PostJSON(ctx, e.baseURL+"/v1/systemone", body, &resp); err != nil {
		return nil, fmt.Errorf("jev: %w", err)
	}
	decide.AddUsage(ctx, resp.Usage.InputTokens, float64(resp.Usage.InputTokens)*costPerInputToken)

	var out []decide.Answer
	for _, q := range req.Questions {
		if a, ok := resp.Answers[q.Name]; ok {
			if ans, ok := fromJev(q, a); ok {
				out = append(out, ans)
			}
		}
	}
	return out, nil
}

func toJev(q decide.Question) question {
	out := question{Instructions: q.Prompt()}
	switch q.Type {
	case decide.Binary:
		out.Type = "noul"
		if len(q.Criteria) > 0 {
			out.Criteria = q.Criteria
		}
	case decide.Choice:
		out.Type = "choice"
		crit := make(map[string]*string, len(q.Criteria))
		for k, v := range q.Criteria {
			if v != "" {
				crit[k] = &v
			} else {
				crit[k] = nil
			}
		}
		out.Criteria = crit
	case decide.Score:
		out.Type = "score"
		out.Criteria = q.Levels
	}
	return out
}

func fromJev(q decide.Question, a answer) (decide.Answer, bool) {
	switch q.Type {
	case decide.Binary:
		if a.Noul == nil {
			return decide.Answer{}, false
		}
		return decide.BinaryAnswer(q, *a.Noul, "jev"), true
	case decide.Choice:
		p := decide.Normalize(a.Probabilities, q.Options())
		if p == nil {
			if a.Choice == "" {
				return decide.Answer{}, false
			}
			p = decide.OneHot(q, a.Choice)
		}
		return withRemote(decide.AnswerFrom(q, p, "jev"), a), true
	case decide.Score:
		// Jev keys score probabilities by level index ("0", "1", ...).
		w := make(map[string]float64, len(q.Levels))
		for k, v := range a.Probabilities {
			if i, err := strconv.Atoi(k); err == nil && i >= 0 && i < len(q.Levels) {
				w[q.Levels[i]] = v
			}
		}
		p := decide.Normalize(w, q.Levels)
		if p == nil {
			return decide.Answer{}, false
		}
		ans := withRemote(decide.AnswerFrom(q, p, "jev"), a)
		if a.Score != nil {
			ans.Score = *a.Score
		}
		return ans, true
	}
	return decide.Answer{}, false
}

// withRemote prefers the API's own calibrated choice/confidence over our recomputation.
func withRemote(ans decide.Answer, a answer) decide.Answer {
	if a.Choice != "" {
		if _, ok := ans.Probabilities[a.Choice]; ok {
			ans.Choice = a.Choice
			ans.Probability = ans.Probabilities[a.Choice]
		}
	}
	if a.Confidence != nil {
		ans.Confidence = *a.Confidence
	}
	return ans
}
