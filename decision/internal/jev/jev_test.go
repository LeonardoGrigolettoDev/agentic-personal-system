package jev

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"

	"aios/decision/internal/decide"
)

func questions() []decide.Question {
	return []decide.Question{
		{Name: "urgent", Type: decide.Binary, Criteria: map[string]string{"true": "deadline today"}},
		{Name: "domain", Type: decide.Choice, Criteria: map[string]string{"engineering": "code", "finance": ""}},
		{Name: "complexity", Type: decide.Score, Levels: []string{"low", "mid", "high"}},
	}
}

func TestDecideMapsRequestAndResponse(t *testing.T) {
	var got map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/systemone" || r.Header.Get("Authorization") != "Bearer k" {
			t.Errorf("path=%s auth=%q", r.URL.Path, r.Header.Get("Authorization"))
		}
		_ = json.NewDecoder(r.Body).Decode(&got)
		_, _ = w.Write([]byte(`{"model":"jev-1.13.0","answers":{
			"urgent":{"type":"noul","noul":0.9},
			"domain":{"type":"choice","choice":"engineering","probabilities":{"engineering":0.97,"finance":0.03},"confidence":0.95},
			"complexity":{"type":"score","score":1.2,"legend":{"0":"low","1":"mid","2":"high"},"probabilities":{"0":0.1,"1":0.6,"2":0.3},"confidence":0.4}
		},"usage":{"input_tokens":1000,"output_tokens":0}}`))
	}))
	defer srv.Close()

	ctx, usage := decide.WithUsage(context.Background())
	answers, err := New(srv.URL, "k", "jev-latest", time.Second).Decide(ctx, decide.Request{State: "ship it", Questions: questions()})
	if err != nil {
		t.Fatal(err)
	}

	qs := got["questions"].(map[string]any)
	if qs["urgent"].(map[string]any)["type"] != "noul" {
		t.Errorf("binary must map to noul: %v", qs["urgent"])
	}
	crit := qs["domain"].(map[string]any)["criteria"].(map[string]any)
	if crit["finance"] != nil || crit["engineering"] != "code" {
		t.Errorf("choice criteria: empty description must be null, got %v", crit)
	}
	if lv := qs["complexity"].(map[string]any)["criteria"].([]any); len(lv) != 3 {
		t.Errorf("score criteria must be the ordered levels: %v", lv)
	}
	if got["model"] != "jev-latest" || got["state"] != "ship it" {
		t.Errorf("model/state not passed through: %v", got)
	}

	if len(answers) != 3 {
		t.Fatalf("want 3 answers, got %d", len(answers))
	}
	if a := answers[0]; a.Probability != 0.9 || a.Choice != "true" {
		t.Errorf("binary: %+v", a)
	}
	if a := answers[1]; a.Choice != "engineering" || a.Confidence != 0.95 {
		t.Errorf("choice: %+v", a)
	}
	if a := answers[2]; a.Choice != "mid" || a.Score != 1.2 || a.Confidence != 0.4 {
		t.Errorf("score: %+v", a)
	}
	if tok, cost := usage.Snapshot(); tok != 1000 || cost <= 0 {
		t.Errorf("usage not recorded: %d %f", tok, cost)
	}
}

func TestRetriesOverloaded(t *testing.T) {
	var calls atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if calls.Add(1) < 3 {
			w.WriteHeader(529)
			return
		}
		_, _ = w.Write([]byte(`{"answers":{"urgent":{"type":"noul","noul":0.2}}}`))
	}))
	defer srv.Close()

	e := New(srv.URL, "k", "jev-latest", time.Second).WithBackoff(time.Millisecond)
	answers, err := e.Decide(context.Background(), decide.Request{State: "x", Questions: questions()[:1]})
	if err != nil || len(answers) != 1 || calls.Load() != 3 {
		t.Fatalf("answers=%v err=%v calls=%d", answers, err, calls.Load())
	}
}

func TestRejectsOversizedState(t *testing.T) {
	big := make([]byte, MaxStateBytes+1)
	for i := range big {
		big[i] = 'a'
	}
	_, err := New("http://unused", "k", "m", time.Second).Decide(context.Background(),
		decide.Request{State: string(big), Questions: questions()[:1]})
	if err == nil {
		t.Fatal("expected size error")
	}
}
