// Package local is the on-device backend: Qwen3 (Ollama) constrained to an enum of short labels.
package local

import (
	"fmt"
	"strings"

	"aios/decision/internal/decide"
)

const letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

// labelSet maps the options of a question to labels the model emits as a single token:
// binary Y/N, score level indices 0-9, choice A-Z then a-z. Choices beyond 52 options fall
// back to the option names themselves (multi-token, so logprobs usually can't be read).
type labelSet struct {
	q        decide.Question
	labels   []string // same order as q.Options()
	toOption map[string]string
}

func newLabelSet(q decide.Question) labelSet {
	opts := q.Options()
	labels := make([]string, len(opts))
	switch {
	case q.Type == decide.Binary:
		labels = []string{"Y", "N"}
	case q.Type == decide.Score:
		for i := range opts {
			labels[i] = fmt.Sprint(i)
		}
	case len(opts) <= len(letters):
		for i := range opts {
			labels[i] = letters[i : i+1]
		}
	default:
		copy(labels, opts)
	}
	ls := labelSet{q: q, labels: labels, toOption: make(map[string]string, len(opts))}
	for i, l := range labels {
		ls.toOption[l] = opts[i]
	}
	return ls
}

// menu renders "label = option: description" lines for the prompt.
func (ls labelSet) menu() string {
	var b strings.Builder
	opts := ls.q.Options()
	for i, l := range ls.labels {
		desc := ""
		switch ls.q.Type {
		case decide.Binary:
			desc = map[string]string{"true": "yes", "false": "no"}[opts[i]]
			if d := ls.q.Criteria[opts[i]]; d != "" {
				desc += ": " + d
			}
		case decide.Choice:
			desc = opts[i]
			if d := ls.q.Criteria[opts[i]]; d != "" {
				desc += ": " + d
			}
		case decide.Score:
			desc = opts[i]
		}
		fmt.Fprintf(&b, "%s = %s\n", l, desc)
	}
	return b.String()
}

// match maps a model token to its label, tolerating JSON punctuation and whitespace
// glued to it (e.g. `":"A`, ` A`, `A"`).
func (ls labelSet) match(token string) (string, bool) {
	l := strings.Trim(token, " \t\r\n\"'{}[]:,")
	_, ok := ls.toOption[l]
	return l, ok
}

// toOptions converts a distribution over labels into one over options.
func (ls labelSet) toOptions(p map[string]float64) map[string]float64 {
	out := make(map[string]float64, len(p))
	for l, v := range p {
		out[ls.toOption[l]] = v
	}
	return out
}

// schema is the JSON schema Ollama/LiteLLM enforce as a grammar: {"answer": <one label>}.
func (ls labelSet) schema() map[string]any {
	return map[string]any{
		"type":                 "object",
		"properties":           map[string]any{"answer": map[string]any{"type": "string", "enum": ls.labels}},
		"required":             []string{"answer"},
		"additionalProperties": false,
	}
}
