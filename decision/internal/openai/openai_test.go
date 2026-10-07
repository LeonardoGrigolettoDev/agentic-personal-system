package openai

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"aios/decision/internal/decide"
)

func TestDecideMapsRequestAndResponse(t *testing.T) {
	var got map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/decisions" {
			t.Errorf("path %s", r.URL.Path)
		}
		_ = json.NewDecoder(r.Body).Decode(&got)
		_, _ = w.Write([]byte(`{"answers":[
			{"type":"predicate","name":"urgent","probability":0.92},
			{"type":"choice","name":"domain","choice":"finance","probabilities":[{"value":"finance","probability":0.8},{"value":"engineering","probability":0.2}],"confidence":0.7},
			{"type":"score","name":"complexity","score":1.1,"probabilities":[{"value":0,"label":"low","probability":0.1},{"value":1,"label":"mid","probability":0.7},{"value":2,"label":"high","probability":0.2}],"confidence":0.55},
			{"type":"refusal","name":"nsfw"}
		]}`))
	}))
	defer srv.Close()

	req := decide.Request{
		State:  "fatura do cartão",
		Images: []string{"data:image/png;base64,AAAA"},
		Questions: []decide.Question{
			{Name: "urgent", Type: decide.Binary, Criteria: map[string]string{"true": "due today"}},
			{Name: "domain", Type: decide.Choice, Criteria: map[string]string{"engineering": "", "finance": "money"}},
			{Name: "complexity", Type: decide.Score, Levels: []string{"low", "mid", "high"}},
			{Name: "nsfw", Type: decide.Binary},
		},
	}
	answers, err := New(srv.URL+"/v1", "k", "gpt-6-luna", time.Second).Decide(context.Background(), req)
	if err != nil {
		t.Fatal(err)
	}

	qs := got["questions"].([]any)
	if q := qs[0].(map[string]any); q["type"] != "predicate" || q["name"] != "urgent" {
		t.Errorf("binary must map to predicate: %v", q)
	}
	if q := qs[1].(map[string]any); len(q["choices"].([]any)) != 2 {
		t.Errorf("choices: %v", q)
	}
	input := got["input"].([]any)[0].(map[string]any)["content"].([]any)
	if input[1].(map[string]any)["type"] != "input_image" {
		t.Errorf("images must be input_image parts: %v", input)
	}

	if len(answers) != 4 {
		t.Fatalf("want 4 answers, got %d: %+v", len(answers), answers)
	}
	if answers[0].Probability != 0.92 {
		t.Errorf("predicate: %+v", answers[0])
	}
	if a := answers[1]; a.Choice != "finance" || a.Confidence != 0.7 {
		t.Errorf("choice: %+v", a)
	}
	if a := answers[2]; a.Choice != "mid" || a.Score != 1.1 {
		t.Errorf("score: %+v", a)
	}
	if !answers[3].Refused {
		t.Errorf("refusal not mapped: %+v", answers[3])
	}
}

func TestStringInputWithoutImages(t *testing.T) {
	var got map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewDecoder(r.Body).Decode(&got)
		_, _ = w.Write([]byte(`{"answers":[]}`))
	}))
	defer srv.Close()
	_, err := New(srv.URL, "k", "m", time.Second).Decide(context.Background(),
		decide.Request{State: "plain", Questions: []decide.Question{{Name: "x", Type: decide.Binary}}})
	if err != nil || got["input"] != "plain" {
		t.Fatalf("input=%v err=%v", got["input"], err)
	}
}
