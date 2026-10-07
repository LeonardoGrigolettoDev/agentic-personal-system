// Package openai maps decisions onto the OpenAI Decisions API (beta).
// https://developers.openai.com/api/docs/guides/decisions - questions are an array with
// "name"; binary is "predicate"; images go in as input_image data URLs.
package openai

import (
	"context"
	"fmt"
	"net/http"
	"strings"
	"time"

	"aios/decision/internal/decide"
	"aios/decision/internal/httpx"
)

const (
	DefaultBaseURL = "https://api.openai.com"
	// USD per input token ($0.10 / 1M) before regional/long-context multipliers.
	costPerInputToken = 0.10 / 1e6
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
		baseURL: strings.TrimSuffix(strings.TrimSuffix(baseURL, "/"), "/v1"),
		model:   model,
		client: &httpx.Client{
			HTTP:       &http.Client{Timeout: timeout},
			Token:      apiKey,
			MaxRetries: 3,
			Backoff:    250 * time.Millisecond,
			RetryOn:    []int{http.StatusTooManyRequests, http.StatusServiceUnavailable},
		},
	}
}

func (e *Engine) WithBackoff(d time.Duration) *Engine { e.client.Backoff = d; return e }

func (e *Engine) Name() string { return "openai" }

type choiceOpt struct {
	Value       string `json:"value"`
	Description string `json:"description,omitempty"`
}

type level struct {
	Label string `json:"label"`
}

type question struct {
	Type         string      `json:"type"`
	Name         string      `json:"name"`
	Instructions string      `json:"instructions"`
	Choices      []choiceOpt `json:"choices,omitempty"`
	Levels       []level     `json:"levels,omitempty"`
}

type contentPart struct {
	Type     string `json:"type"`
	Text     string `json:"text,omitempty"`
	ImageURL string `json:"image_url,omitempty"`
}

type message struct {
	Role    string        `json:"role"`
	Content []contentPart `json:"content"`
}

type request struct {
	Model     string     `json:"model"`
	Input     any        `json:"input"`
	Questions []question `json:"questions"`
}

type probability struct {
	Value       any     `json:"value"` // string for choice, level index for score
	Label       string  `json:"label"`
	Probability float64 `json:"probability"`
}

type answer struct {
	Type          string        `json:"type"`
	Name          string        `json:"name"`
	Probability   *float64      `json:"probability"`
	Choice        string        `json:"choice"`
	Score         *float64      `json:"score"`
	Probabilities []probability `json:"probabilities"`
	Confidence    *float64      `json:"confidence"`
}

type response struct {
	Answers []answer `json:"answers"`
	Usage   struct {
		InputTokens int `json:"input_tokens"`
	} `json:"usage"`
}

func (e *Engine) Decide(ctx context.Context, req decide.Request) ([]decide.Answer, error) {
	body := request{Model: e.model, Input: input(req)}
	for _, q := range req.Questions {
		body.Questions = append(body.Questions, toOpenAI(q))
	}
	var resp response
	if err := e.client.PostJSON(ctx, e.baseURL+"/v1/decisions", body, &resp); err != nil {
		return nil, fmt.Errorf("openai: %w", err)
	}
	decide.AddUsage(ctx, resp.Usage.InputTokens, float64(resp.Usage.InputTokens)*costPerInputToken)

	byName := make(map[string]answer, len(resp.Answers))
	for _, a := range resp.Answers {
		byName[a.Name] = a
	}
	var out []decide.Answer
	for _, q := range req.Questions {
		if a, ok := byName[q.Name]; ok {
			if ans, ok := fromOpenAI(q, a); ok {
				out = append(out, ans)
			}
		}
	}
	return out, nil
}

func input(req decide.Request) any {
	text := decide.StateText(req.State)
	if len(req.Images) == 0 {
		return text
	}
	parts := []contentPart{{Type: "input_text", Text: text}}
	for _, img := range req.Images {
		parts = append(parts, contentPart{Type: "input_image", ImageURL: img})
	}
	return []message{{Role: "user", Content: parts}}
}

func toOpenAI(q decide.Question) question {
	out := question{Name: q.Name, Instructions: q.Prompt()}
	switch q.Type {
	case decide.Binary:
		// predicate has no criteria field; fold the descriptions into the instructions
		out.Type = "predicate"
		if t := q.Criteria["true"]; t != "" {
			out.Instructions += "\nTrue when: " + t
		}
		if f := q.Criteria["false"]; f != "" {
			out.Instructions += "\nFalse when: " + f
		}
	case decide.Choice:
		out.Type = "choice"
		for _, o := range q.Options() {
			out.Choices = append(out.Choices, choiceOpt{Value: o, Description: q.Criteria[o]})
		}
	case decide.Score:
		out.Type = "score"
		for _, l := range q.Levels {
			out.Levels = append(out.Levels, level{Label: l})
		}
	}
	return out
}

func fromOpenAI(q decide.Question, a answer) (decide.Answer, bool) {
	if a.Type == "refusal" {
		return decide.Answer{Name: q.Name, Type: q.Type, Backend: "openai", Refused: true}, true
	}
	var ans decide.Answer
	switch q.Type {
	case decide.Binary:
		if a.Probability == nil {
			return ans, false
		}
		return decide.BinaryAnswer(q, *a.Probability, "openai"), true
	case decide.Choice:
		w := map[string]float64{}
		for _, p := range a.Probabilities {
			if s, ok := p.Value.(string); ok {
				w[s] = p.Probability
			}
		}
		p := decide.Normalize(w, q.Options())
		if p == nil {
			if _, ok := q.Criteria[a.Choice]; !ok {
				return ans, false
			}
			p = decide.OneHot(q, a.Choice)
		}
		ans = decide.AnswerFrom(q, p, "openai")
		if _, ok := q.Criteria[a.Choice]; ok {
			ans.Choice, ans.Probability = a.Choice, p[a.Choice]
		}
	case decide.Score:
		w := map[string]float64{}
		for _, p := range a.Probabilities {
			if i, ok := p.Value.(float64); ok && i >= 0 && int(i) < len(q.Levels) {
				w[q.Levels[int(i)]] = p.Probability
			} else if p.Label != "" {
				w[p.Label] = p.Probability
			}
		}
		p := decide.Normalize(w, q.Levels)
		if p == nil {
			if a.Score == nil {
				return ans, false
			}
			i := min(max(int(*a.Score+0.5), 0), len(q.Levels)-1)
			p = decide.OneHot(q, q.Levels[i])
		}
		ans = decide.AnswerFrom(q, p, "openai")
		if a.Score != nil {
			ans.Score = *a.Score
		}
	default:
		return ans, false
	}
	if a.Confidence != nil {
		ans.Confidence = *a.Confidence
	}
	return ans, true
}
