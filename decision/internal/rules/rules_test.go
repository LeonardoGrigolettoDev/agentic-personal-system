package rules

import (
	"context"
	"io"
	"log/slog"
	"os"
	"testing"

	"aios/decision/internal/decide"
)

var quiet = slog.New(slog.NewTextHandler(io.Discard, nil))

var domains = map[string]string{
	"chief": "", "engineering": "", "finance": "", "projects": "", "personal": "", "learning": "",
}

func questions() []decide.Question {
	return []decide.Question{
		{Name: "domain", Type: decide.Choice, Criteria: domains},
		{Name: "needs_confirmation", Type: decide.Binary},
		{Name: "complexity", Type: decide.Score, Levels: []string{"trivial", "simple", "medium", "hard", "critical"}},
	}
}

// The real config shipped in the repo must compile and route.
func TestRepoRulesFile(t *testing.T) {
	path := "../../../config/decision/rules.yaml"
	if _, err := os.Stat(path); err != nil {
		t.Skip("repo rules.yaml not available:", err)
	}
	e, err := Load(path, quiet)
	if err != nil {
		t.Fatal(err)
	}
	cases := []struct {
		state       any
		wantDomain  string
		wantConfirm bool
	}{
		{map[string]any{"text": "Preciso pagar o boleto da fatura do cartão"}, "finance", true},
		{map[string]any{"text": "O deploy quebrou com um stack trace no endpoint /users"}, "engineering", false},
		{"Quero estudar para a prova de cálculo", "learning", false}, // plain string state
		{map[string]any{"text": "bom dia"}, "", false},
		{map[string]any{"title": "sem campo text"}, "", false}, // no 'text': must not error
		{map[string]any{"text": 42}, "", false},                // wrong type: must not error
		{[]any{"array", "state"}, "", false},
	}
	for _, tc := range cases {
		answers, err := e.Decide(context.Background(), decide.Request{State: tc.state, Questions: questions()})
		if err != nil {
			t.Fatalf("%v: %v", tc.state, err)
		}
		got := map[string]decide.Answer{}
		for _, a := range answers {
			got[a.Name] = a
		}
		if d, ok := got["domain"]; ok != (tc.wantDomain != "") || d.Choice != tc.wantDomain {
			t.Errorf("%v: domain=%+v want %q", tc.state, d, tc.wantDomain)
		}
		if c, ok := got["needs_confirmation"]; ok != tc.wantConfirm {
			t.Errorf("%v: confirmation=%+v want %v", tc.state, c, tc.wantConfirm)
		}
		if _, ok := got["complexity"]; ok {
			t.Errorf("no rule targets complexity")
		}
	}
}

func TestMatchAnswersWithFullConfidence(t *testing.T) {
	e, err := Parse([]byte(`
rules:
  - {name: big, question: complexity, when: 'len(state.files) > 10', answer: hard}
  - {name: invalid-answer, question: domain, when: 'true', answer: not-a-domain}
  - {name: first, question: domain, when: 'state.repo != nil', answer: engineering}
  - {name: second, question: domain, when: 'true', answer: chief}
`), quiet)
	if err != nil {
		t.Fatal(err)
	}
	state := map[string]any{"repo": "aios", "files": make([]any, 12)}
	answers, _ := e.Decide(context.Background(), decide.Request{State: state, Questions: questions()})
	got := map[string]decide.Answer{}
	for _, a := range answers {
		got[a.Name] = a
	}
	if a := got["domain"]; a.Choice != "engineering" || a.Confidence != 1 || a.Backend != "rules" {
		t.Fatalf("first valid matching rule wins: %+v", a)
	}
	if a := got["complexity"]; a.Choice != "hard" || a.Score != 3 || a.Probabilities["trivial"] != 0 {
		t.Fatalf("score: %+v", a)
	}
}

func TestParseErrors(t *testing.T) {
	for name, src := range map[string]string{
		"syntax":   "rules:\n  - {name: x, question: q, when: 'state.text matches', answer: a}\n",
		"not bool": "rules:\n  - {name: x, question: q, when: '1 + 1', answer: a}\n",
		"missing":  "rules:\n  - {name: x, when: 'true'}\n",
		"yaml":     "rules: [",
	} {
		if _, err := Parse([]byte(src), quiet); err == nil {
			t.Errorf("%s: expected error", name)
		}
	}
}
