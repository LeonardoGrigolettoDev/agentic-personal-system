package local

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"net/http"
	"strings"
	"sync"
	"time"

	"aios/decision/internal/decide"
	"aios/decision/internal/httpx"
)

const (
	ModeLogprobs = "logprobs"
	ModeVote     = "vote"

	maxStateChars = 12000 // decider-local has a 4k-token window
	maxTopLogprob = 20
	parallel      = 2 // Ollama serializes anyway; this just overlaps prompt building/IO
	votes         = 3
	voteTemp      = 0.7
	// Confidence used when the model gave an answer but no logprobs at all: below the
	// default threshold, so the cascade keeps looking for a calibrated answer.
	uncalibratedConfidence = 0.5

	systemPrompt = "You are a precise decision classifier. Read the STATE and answer each QUESTION " +
		"with exactly one of its labels. Reply only with the JSON object requested."
)

type Config struct {
	Mode          string
	OllamaBaseURL string
	Model         string // Ollama model tag (logprobs mode)
	LiteLLMURL    string
	LiteLLMKey    string
	LiteLLMModel  string // LiteLLM alias (vote mode)
	Timeout       time.Duration
}

type Engine struct {
	cfg  Config
	http *httpx.Client
}

func New(cfg Config) (*Engine, error) {
	switch cfg.Mode {
	case ModeLogprobs, ModeVote:
	default:
		return nil, fmt.Errorf("local: unknown mode %q (logprobs|vote)", cfg.Mode)
	}
	if cfg.LiteLLMModel == "" {
		cfg.LiteLLMModel = "decider-local"
	}
	token := ""
	if cfg.Mode == ModeVote {
		token = cfg.LiteLLMKey
	}
	return &Engine{cfg: cfg, http: &httpx.Client{HTTP: &http.Client{Timeout: cfg.Timeout}, Token: token}}, nil
}

func (e *Engine) Name() string { return "local" }

func (e *Engine) Decide(ctx context.Context, req decide.Request) ([]decide.Answer, error) {
	state := decide.StateText(req.State)
	if len(state) > maxStateChars {
		state = state[:maxStateChars] + "\n[...truncated]"
	}
	if e.cfg.Mode == ModeVote {
		return e.vote(ctx, state, req.Questions)
	}
	return e.logprobs(ctx, state, req.Questions)
}

// ---- logprobs mode: one native Ollama /api/chat call per question ----

type ollamaRequest struct {
	Model       string         `json:"model"`
	Messages    []chatMessage  `json:"messages"`
	Stream      bool           `json:"stream"`
	Think       bool           `json:"think"`
	Format      map[string]any `json:"format"`
	Logprobs    bool           `json:"logprobs"`
	TopLogprobs int            `json:"top_logprobs"`
	Options     map[string]any `json:"options"`
}

type chatMessage struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

type tokenLogprob struct {
	Token       string  `json:"token"`
	Logprob     float64 `json:"logprob"`
	TopLogprobs []struct {
		Token   string  `json:"token"`
		Logprob float64 `json:"logprob"`
	} `json:"top_logprobs"`
}

type ollamaResponse struct {
	Message         chatMessage    `json:"message"`
	Logprobs        []tokenLogprob `json:"logprobs"`
	PromptEvalCount int            `json:"prompt_eval_count"`
}

func (e *Engine) logprobs(ctx context.Context, state string, qs []decide.Question) ([]decide.Answer, error) {
	var (
		mu   sync.Mutex
		wg   sync.WaitGroup
		out  []decide.Answer
		errs []error
		sem  = make(chan struct{}, parallel)
	)
	for _, q := range qs {
		wg.Go(func() {
			sem <- struct{}{}
			defer func() { <-sem }()
			a, err := e.askOllama(ctx, state, q)
			mu.Lock()
			defer mu.Unlock()
			if err != nil {
				errs = append(errs, fmt.Errorf("%s: %w", q.Name, err))
				return
			}
			out = append(out, a)
		})
	}
	wg.Wait()
	if len(out) == 0 && len(errs) > 0 {
		return nil, errors.Join(errs...)
	}
	return out, nil
}

func (e *Engine) askOllama(ctx context.Context, state string, q decide.Question) (decide.Answer, error) {
	ls := newLabelSet(q)
	body := ollamaRequest{
		Model: e.cfg.Model,
		Messages: []chatMessage{
			{Role: "system", Content: systemPrompt},
			{Role: "user", Content: fmt.Sprintf("STATE:\n%s\n\nQUESTION: %s\nLABELS:\n%sAnswer as {\"answer\": \"<label>\"}.",
				state, q.Prompt(), ls.menu())},
		},
		Format:      ls.schema(),
		Logprobs:    true,
		TopLogprobs: min(max(len(ls.labels)+2, 5), maxTopLogprob),
		Options:     map[string]any{"temperature": 0},
	}
	var resp ollamaResponse
	if err := e.http.PostJSON(ctx, strings.TrimSuffix(e.cfg.OllamaBaseURL, "/")+"/api/chat", body, &resp); err != nil {
		return decide.Answer{}, err
	}
	decide.AddUsage(ctx, resp.PromptEvalCount, 0)
	return answerFromLogprobs(ls, resp)
}

// answerFromLogprobs turns one Ollama response into an answer. With top_logprobs at the label
// position the distribution is a softmax renormalized over the valid labels; otherwise the
// chosen label gets probability 1 and the confidence is the margin 2p-1 derived from the
// chosen token's own logprob (or uncalibratedConfidence when none is reported).
func answerFromLogprobs(ls labelSet, resp ollamaResponse) (decide.Answer, error) {
	var content struct {
		Answer string `json:"answer"`
	}
	_ = json.Unmarshal([]byte(resp.Message.Content), &content)

	pos, label := labelPosition(ls, resp.Logprobs)
	if pos >= 0 && len(resp.Logprobs[pos].TopLogprobs) > 0 {
		grouped := map[string][]float64{label: {resp.Logprobs[pos].Logprob}}
		for _, alt := range resp.Logprobs[pos].TopLogprobs {
			if l, ok := ls.match(alt.Token); ok && alt.Token != resp.Logprobs[pos].Token {
				grouped[l] = append(grouped[l], alt.Logprob)
			}
		}
		if p := decide.SoftmaxLogprobs(grouped, ls.labels); p != nil {
			return decide.AnswerFrom(ls.q, ls.toOptions(p), "local"), nil
		}
	}

	chosen := content.Answer
	if _, ok := ls.toOption[chosen]; !ok {
		chosen = label
	}
	opt, ok := ls.toOption[chosen]
	if !ok {
		return decide.Answer{}, fmt.Errorf("unparseable answer %q", resp.Message.Content)
	}
	a := decide.AnswerFrom(ls.q, decide.OneHot(ls.q, opt), "local")
	a.Confidence = uncalibratedConfidence
	if pos >= 0 && label == chosen {
		a.Confidence = min(max(2*math.Exp(resp.Logprobs[pos].Logprob)-1, 0), 1)
	}
	return a, nil
}

// labelPosition finds the first generated token that is a label, skipping the JSON scaffolding
// before the value (`{"answer":"`): only tokens from the first ':' onward are considered.
func labelPosition(ls labelSet, lps []tokenLogprob) (int, string) {
	seenColon := !strings.Contains(joinTokens(lps), ":")
	for i, t := range lps {
		tok := t.Token
		if !seenColon {
			j := strings.LastIndex(tok, ":")
			if j < 0 {
				continue
			}
			seenColon, tok = true, tok[j+1:]
		}
		if l, ok := ls.match(tok); ok {
			return i, l
		}
	}
	return -1, ""
}

func joinTokens(lps []tokenLogprob) string {
	var b strings.Builder
	for _, t := range lps {
		b.WriteString(t.Token)
	}
	return b.String()
}

// ---- vote mode: k samples via LiteLLM at temperature 0.7, vote share = probability ----

type completionRequest struct {
	Model          string         `json:"model"`
	Messages       []chatMessage  `json:"messages"`
	Temperature    float64        `json:"temperature"`
	ResponseFormat map[string]any `json:"response_format"`
}

type completionResponse struct {
	Choices []struct {
		Message chatMessage `json:"message"`
	} `json:"choices"`
	Usage struct {
		PromptTokens int `json:"prompt_tokens"`
	} `json:"usage"`
}

func (e *Engine) vote(ctx context.Context, state string, qs []decide.Question) ([]decide.Answer, error) {
	sets := make([]labelSet, len(qs))
	props := map[string]any{}
	var prompt strings.Builder
	fmt.Fprintf(&prompt, "STATE:\n%s\n", state)
	for i, q := range qs {
		sets[i] = newLabelSet(q)
		props[q.Name] = map[string]any{"type": "string", "enum": sets[i].labels}
		fmt.Fprintf(&prompt, "\nQUESTION %q: %s\nLABELS:\n%s", q.Name, q.Prompt(), sets[i].menu())
	}
	prompt.WriteString("\nAnswer with a JSON object mapping each question name to one of its labels.")
	required := make([]string, len(qs))
	for i, q := range qs {
		required[i] = q.Name
	}
	body := completionRequest{
		Model:       e.cfg.LiteLLMModel,
		Temperature: voteTemp,
		Messages:    []chatMessage{{Role: "system", Content: systemPrompt}, {Role: "user", Content: prompt.String()}},
		ResponseFormat: map[string]any{"type": "json_schema", "json_schema": map[string]any{
			"name": "decision", "strict": true,
			"schema": map[string]any{"type": "object", "properties": props, "required": required, "additionalProperties": false},
		}},
	}

	samples := make([]map[string]string, votes)
	errs := make([]error, votes)
	var wg sync.WaitGroup
	for i := range votes {
		wg.Go(func() {
			var resp completionResponse
			url := strings.TrimSuffix(e.cfg.LiteLLMURL, "/") + "/v1/chat/completions"
			if errs[i] = e.http.PostJSON(ctx, url, body, &resp); errs[i] != nil {
				return
			}
			decide.AddUsage(ctx, resp.Usage.PromptTokens, 0)
			if len(resp.Choices) == 0 {
				errs[i] = errors.New("no choices")
				return
			}
			errs[i] = json.Unmarshal([]byte(resp.Choices[0].Message.Content), &samples[i])
		})
	}
	wg.Wait()
	return tally(sets, samples, errors.Join(errs...))
}

// tally turns votes into answers; a question nobody voted validly for is left unanswered.
func tally(sets []labelSet, samples []map[string]string, err error) ([]decide.Answer, error) {
	var out []decide.Answer
	for _, ls := range sets {
		counts := map[string]float64{}
		for _, s := range samples {
			if l, ok := s[ls.q.Name]; ok {
				if _, valid := ls.toOption[l]; valid {
					counts[l]++
				}
			}
		}
		if p := decide.Normalize(counts, ls.labels); p != nil {
			out = append(out, decide.AnswerFrom(ls.q, ls.toOptions(p), "local"))
		}
	}
	if len(out) == 0 && err != nil {
		return nil, err
	}
	return out, nil
}
