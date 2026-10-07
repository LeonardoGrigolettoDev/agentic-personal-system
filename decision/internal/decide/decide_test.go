package decide

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"math"
	"strings"
	"testing"
)

func validRequest() Request {
	return Request{
		State: map[string]any{"text": "corrigir bug no deploy"},
		Questions: []Question{
			{Name: "domain", Type: Choice, Criteria: map[string]string{"engineering": "", "finance": ""}},
			{Name: "urgent", Type: Binary},
			{Name: "complexity", Type: Score, Levels: []string{"low", "mid", "high"}},
		},
	}
}

func TestValidate(t *testing.T) {
	manyOpts := map[string]string{}
	for i := range 256 {
		manyOpts[strings.Repeat("x", i+1)] = ""
	}
	cases := map[string]struct {
		mutate  func(*Request)
		wantErr string
	}{
		"ok":               {func(*Request) {}, ""},
		"no state":         {func(r *Request) { r.State = nil }, "state is required"},
		"no questions":     {func(r *Request) { r.Questions = nil }, "at least one question"},
		"duplicate name":   {func(r *Request) { r.Questions[1].Name = "domain"; r.Questions[1].Type = Binary }, "duplicate"},
		"bad name":         {func(r *Request) { r.Questions[0].Name = "has space" }, "must match"},
		"bad type":         {func(r *Request) { r.Questions[1].Type = "maybe" }, "type must be"},
		"choice one opt":   {func(r *Request) { r.Questions[0].Criteria = map[string]string{"a": ""} }, "2..255"},
		"choice 256 opts":  {func(r *Request) { r.Questions[0].Criteria = manyOpts }, "2..255"},
		"binary bad key":   {func(r *Request) { r.Questions[1].Criteria = map[string]string{"yes": "x"} }, "true"},
		"score one level":  {func(r *Request) { r.Questions[2].Levels = []string{"x"} }, "2..10"},
		"score 11 levels":  {func(r *Request) { r.Questions[2].Levels = strings.Split("a,b,c,d,e,f,g,h,i,j,k", ",") }, "2..10"},
		"score dup level":  {func(r *Request) { r.Questions[2].Levels = []string{"a", "a"} }, "unique"},
		"levels on choice": {func(r *Request) { r.Questions[0].Levels = []string{"a", "b"} }, "only valid for score"},
		"threshold > 1":    {func(r *Request) { r.Threshold = 1.5 }, "threshold"},
		"bad run id":       {func(r *Request) { r.RunID = "42" }, "UUID"},
		"remote image":     {func(r *Request) { r.Images = []string{"https://x/y.png"} }, "remote URLs"},
	}
	for name, tc := range cases {
		t.Run(name, func(t *testing.T) {
			r := validRequest()
			tc.mutate(&r)
			err := r.Validate()
			if tc.wantErr == "" {
				if err != nil {
					t.Fatalf("unexpected error: %v", err)
				}
				return
			}
			if err == nil || !strings.Contains(err.Error(), tc.wantErr) || !IsValidation(err) {
				t.Fatalf("want validation error containing %q, got %v", tc.wantErr, err)
			}
		})
	}
}

func TestValidateNormalizesRawBase64Image(t *testing.T) {
	r := validRequest()
	// 1x1 PNG
	png := "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
	r.Images = []string{png}
	if err := r.Validate(); err != nil {
		t.Fatal(err)
	}
	if want := "data:image/png;base64," + png; r.Images[0] != want {
		t.Fatalf("got %q", r.Images[0][:40])
	}
}

func TestConfidence(t *testing.T) {
	if c := Confidence(map[string]float64{"a": 1, "b": 0}, 2); c != 1 {
		t.Fatalf("one-hot: %v", c)
	}
	if c := Confidence(map[string]float64{"a": .5, "b": .5}, 2); math.Abs(c) > 1e-12 {
		t.Fatalf("uniform: %v", c)
	}
	// 0.9/0.1 binary: 1 - H/ln2 = 1 - 0.4690 = 0.531
	if c := Confidence(map[string]float64{"a": .9, "b": .1}, 2); math.Abs(c-0.531) > 1e-3 {
		t.Fatalf("0.9/0.1: %v", c)
	}
	if c := Confidence(map[string]float64{"a": 1}, 1); c != 1 {
		t.Fatalf("single option: %v", c)
	}
}

func TestSoftmaxLogprobs(t *testing.T) {
	opts := []string{"A", "B", "C"}
	// "A" appears as two tokens ("A" and " A"); "Z" is not a valid option and must be ignored.
	p := SoftmaxLogprobs(map[string][]float64{
		"A": {math.Log(0.6), math.Log(0.1)},
		"B": {math.Log(0.2)},
		"Z": {math.Log(0.5)},
	}, opts)
	want := map[string]float64{"A": 0.7 / 0.9, "B": 0.2 / 0.9, "C": 0}
	for k, v := range want {
		if math.Abs(p[k]-v) > 1e-9 {
			t.Fatalf("p[%s]=%v want %v", k, p[k], v)
		}
	}
	if SoftmaxLogprobs(map[string][]float64{"Z": {0}}, opts) != nil {
		t.Fatal("expected nil when no valid option has mass")
	}
}

func TestAnswerFromScore(t *testing.T) {
	q := Question{Name: "c", Type: Score, Levels: []string{"low", "mid", "high"}}
	a := AnswerFrom(q, map[string]float64{"low": .1, "mid": .6, "high": .3}, "x")
	if a.Choice != "mid" || math.Abs(a.Score-1.2) > 1e-9 || a.Probability != .6 {
		t.Fatalf("%+v", a)
	}
	b := BinaryAnswer(Question{Name: "u", Type: Binary}, 0.8, "x")
	if b.Choice != "true" || b.Probability != 0.8 || math.Abs(b.Probabilities["false"]-0.2) > 1e-9 {
		t.Fatalf("%+v", b)
	}
}

func TestRequestHashIgnoresKeyOrder(t *testing.T) {
	a := validRequest()
	b := validRequest()
	b.State = map[string]any{"text": "corrigir bug no deploy"}
	if RequestHash(a) != RequestHash(b) {
		t.Fatal("hash must be canonical")
	}
	b.State = "other"
	if RequestHash(a) == RequestHash(b) {
		t.Fatal("hash must change with state")
	}
	if CacheKey(a, 0.8) == CacheKey(a, 0.9) {
		t.Fatal("cache key must depend on threshold")
	}
}

// ---- cascade ----

type fakeEngine struct {
	name    string
	answers map[string]Answer
	err     error
	asked   [][]string
}

func (f *fakeEngine) Name() string { return f.name }

func (f *fakeEngine) Decide(_ context.Context, r Request) ([]Answer, error) {
	f.asked = append(f.asked, names(r.Questions))
	if f.err != nil {
		return nil, f.err
	}
	var out []Answer
	for _, q := range r.Questions {
		if a, ok := f.answers[q.Name]; ok {
			a.Name, a.Type = q.Name, q.Type
			out = append(out, a)
		}
	}
	return out, nil
}

var quiet = slog.New(slog.NewTextHandler(io.Discard, nil))

func TestCascadeForwardsOnlyUnresolved(t *testing.T) {
	rules := &fakeEngine{name: "rules", answers: map[string]Answer{"domain": {Choice: "engineering", Confidence: 1}}}
	local := &fakeEngine{name: "local", answers: map[string]Answer{
		"urgent":     {Choice: "true", Confidence: 0.95},
		"complexity": {Choice: "mid", Confidence: 0.4},
	}}
	jev := &fakeEngine{name: "jev", answers: map[string]Answer{"complexity": {Choice: "high", Confidence: 0.9}}}
	c := NewCascade([]DecisionEngine{rules, local, jev}, []string{"rules", "local", "jev"}, 0.8, quiet)

	answers, trace, err := c.DecideTrace(context.Background(), validRequest())
	if err != nil {
		t.Fatal(err)
	}
	if got := local.asked[0]; strings.Join(got, ",") != "urgent,complexity" {
		t.Fatalf("local asked %v", got)
	}
	if got := jev.asked[0]; strings.Join(got, ",") != "complexity" {
		t.Fatalf("jev asked %v", got)
	}
	want := map[string]string{"domain": "rules", "urgent": "local", "complexity": "jev"}
	for _, a := range answers {
		if a.Backend != want[a.Name] || a.NeedsHuman {
			t.Fatalf("%s answered by %s (needs_human=%v)", a.Name, a.Backend, a.NeedsHuman)
		}
	}
	if len(trace) != 3 || trace[2].Accepted[0] != "complexity" {
		t.Fatalf("trace %+v", trace)
	}
}

func TestCascadeNeedsHumanWithBestAnswer(t *testing.T) {
	broken := &fakeEngine{name: "jev", err: errors.New("boom")}
	local := &fakeEngine{name: "local", answers: map[string]Answer{
		"domain": {Choice: "finance", Confidence: 0.3},
		"urgent": {Choice: "true", Confidence: 0.5},
	}}
	second := &fakeEngine{name: "openai", answers: map[string]Answer{
		"domain": {Choice: "engineering", Confidence: 0.6},
		"urgent": {Refused: true},
	}}
	c := NewCascade([]DecisionEngine{local, broken, second}, []string{"local", "jev", "openai", "missing"}, 0.8, quiet)
	answers, trace, err := c.DecideTrace(context.Background(), validRequest())
	if err != nil {
		t.Fatal(err)
	}
	got := map[string]Answer{}
	for _, a := range answers {
		got[a.Name] = a
		if !a.NeedsHuman {
			t.Fatalf("%s should need a human", a.Name)
		}
	}
	if got["domain"].Choice != "engineering" || got["domain"].Backend != "openai" {
		t.Fatalf("best domain answer: %+v", got["domain"])
	}
	if got["urgent"].Backend != "local" || got["urgent"].Refused {
		t.Fatalf("a refusal must not beat a real answer: %+v", got["urgent"])
	}
	if got["complexity"].Backend != "none" {
		t.Fatalf("unanswered: %+v", got["complexity"])
	}
	if trace[1].Error == "" {
		t.Fatal("backend error should be in the trace")
	}
	if b, minC, nh := Summary(answers); b != "cascade" || minC != 0 || !nh {
		t.Fatalf("summary %s %v %v", b, minC, nh)
	}
}

func TestCascadePlan(t *testing.T) {
	e := func(n string) DecisionEngine { return &fakeEngine{name: n} }
	c := NewCascade([]DecisionEngine{e("rules"), e("local"), e("openai")}, []string{"rules", "local"}, 0.8, quiet)

	r := validRequest()
	r.Images = []string{"data:image/png;base64,AAAA"}
	if plan, _ := c.Plan(r); strings.Join(plan, ",") != "openai,rules,local" {
		t.Fatalf("images should prefer openai: %v", plan)
	}
	r.Images = nil
	r.Backends = []string{"local", "local"}
	if plan, _ := c.Plan(r); strings.Join(plan, ",") != "local" {
		t.Fatalf("override: %v", plan)
	}
	r.Backends = []string{"jev"}
	if _, err := c.Plan(r); !IsValidation(err) {
		t.Fatalf("unknown backend should be a validation error, got %v", err)
	}
	r.Backends, r.Threshold = nil, 0.5
	if c.Threshold(r) != 0.5 {
		t.Fatal("request threshold should override default")
	}
}
