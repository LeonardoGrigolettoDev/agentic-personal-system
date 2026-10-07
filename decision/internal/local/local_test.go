package local

import (
	"context"
	"encoding/json"
	"errors"
	"math"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"aios/decision/internal/decide"
)

var domainQ = decide.Question{Name: "domain", Type: decide.Choice,
	Criteria: map[string]string{"engineering": "code", "finance": "money", "learning": "study"}}

func lp(tok string, p float64, alts map[string]float64) tokenLogprob {
	t := tokenLogprob{Token: tok, Logprob: math.Log(p)}
	for a, ap := range alts {
		t.TopLogprobs = append(t.TopLogprobs, struct {
			Token   string  `json:"token"`
			Logprob float64 `json:"logprob"`
		}{a, math.Log(ap)})
	}
	return t
}

func TestLabelsAreSingleTokens(t *testing.T) {
	ls := newLabelSet(domainQ) // options sorted: engineering, finance, learning
	if ls.labels[0] != "A" || ls.toOption["B"] != "finance" {
		t.Fatalf("labels %v", ls.labels)
	}
	if b := newLabelSet(decide.Question{Name: "x", Type: decide.Binary}); b.toOption["Y"] != "true" {
		t.Fatalf("binary labels %v", b.labels)
	}
	if s := newLabelSet(decide.Question{Name: "s", Type: decide.Score, Levels: []string{"lo", "hi"}}); s.toOption["1"] != "hi" {
		t.Fatalf("score labels %v", s.labels)
	}
}

func TestAnswerFromLogprobsSkipsJSONScaffolding(t *testing.T) {
	ls := newLabelSet(domainQ)
	resp := ollamaResponse{
		Message: chatMessage{Content: `{"answer":"B"}`},
		Logprobs: []tokenLogprob{
			lp(`{"`, 1, nil), lp("answer", 1, nil), lp(`":"`, 1, nil),
			lp("B", 0.7, map[string]float64{"B": 0.7, "A": 0.2, "C": 0.1}),
			lp(`"}`, 1, nil),
		},
	}
	a, err := answerFromLogprobs(ls, resp)
	if err != nil {
		t.Fatal(err)
	}
	if a.Choice != "finance" || math.Abs(a.Probability-0.7) > 1e-9 {
		t.Fatalf("got %+v", a)
	}
	if a.Confidence <= 0 || a.Confidence >= 1 {
		t.Fatalf("confidence should be in (0,1): %f", a.Confidence)
	}
}

func TestAnswerWithoutLogprobsIsUncalibrated(t *testing.T) {
	a, err := answerFromLogprobs(newLabelSet(domainQ), ollamaResponse{Message: chatMessage{Content: `{"answer":"C"}`}})
	if err != nil || a.Choice != "learning" || a.Confidence != uncalibratedConfidence {
		t.Fatalf("a=%+v err=%v", a, err)
	}
	if _, err := answerFromLogprobs(newLabelSet(domainQ), ollamaResponse{Message: chatMessage{Content: `garbage`}}); err == nil {
		t.Fatal("expected error for unparseable answer")
	}
}

func TestOllamaRequestShape(t *testing.T) {
	var got map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/chat" {
			t.Errorf("path %s", r.URL.Path)
		}
		_ = json.NewDecoder(r.Body).Decode(&got)
		_ = json.NewEncoder(w).Encode(ollamaResponse{
			Message:  chatMessage{Content: `{"answer":"A"}`},
			Logprobs: []tokenLogprob{lp(`":"`, 1, nil), lp("A", 0.99, map[string]float64{"A": 0.99, "B": 0.01})},
		})
	}))
	defer srv.Close()

	e, _ := New(Config{Mode: ModeLogprobs, OllamaBaseURL: srv.URL, Model: "qwen3:4b", Timeout: time.Second})
	answers, err := e.Decide(context.Background(), decide.Request{State: "bug no deploy", Questions: []decide.Question{domainQ}})
	if err != nil || len(answers) != 1 || answers[0].Choice != "engineering" {
		t.Fatalf("answers=%+v err=%v", answers, err)
	}
	if got["think"] != false || got["stream"] != false || got["logprobs"] != true {
		t.Errorf("think/stream/logprobs flags wrong: %v", got)
	}
	enum := got["format"].(map[string]any)["properties"].(map[string]any)["answer"].(map[string]any)["enum"].([]any)
	if len(enum) != 3 {
		t.Errorf("format enum %v", enum)
	}
}

func TestTallyVoteShares(t *testing.T) {
	ls := newLabelSet(domainQ)
	samples := []map[string]string{{"domain": "A"}, {"domain": "A"}, {"domain": "Z"}}
	out, err := tally([]labelSet{ls}, samples, nil)
	if err != nil || len(out) != 1 {
		t.Fatalf("out=%v err=%v", out, err)
	}
	if out[0].Choice != "engineering" || out[0].Probability != 1 {
		t.Fatalf("invalid label must not count: %+v", out[0])
	}
	if _, err := tally([]labelSet{ls}, []map[string]string{{}}, errors.New("boom")); err == nil {
		t.Fatal("expected error when nothing was answered")
	}
}

func TestUnknownMode(t *testing.T) {
	if _, err := New(Config{Mode: "magic"}); err == nil {
		t.Fatal("expected error")
	}
}
