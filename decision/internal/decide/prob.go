package decide

import (
	"math"
	"slices"
)

// Confidence is 1 - H(p)/ln(K): the normalized Shannon entropy of the answer distribution
// over its K valid options, flipped so that a one-hot distribution scores 1 and a uniform one 0.
// K counts every valid option (including those with probability 0), so the scale is comparable
// across questions with different numbers of options. K <= 1 is trivially certain.
func Confidence(p map[string]float64, k int) float64 {
	if k <= 1 {
		return 1
	}
	var h float64
	for _, v := range p {
		if v > 0 {
			h -= v * math.Log(v)
		}
	}
	return clamp01(1 - h/math.Log(float64(k)))
}

// Normalize rescales non-negative weights over options to sum to 1. Options missing
// from w get 0. If every weight is 0 it returns nil.
func Normalize(w map[string]float64, options []string) map[string]float64 {
	var sum float64
	for _, o := range options {
		if v := w[o]; v > 0 {
			sum += v
		}
	}
	if sum == 0 {
		return nil
	}
	p := make(map[string]float64, len(options))
	for _, o := range options {
		p[o] = max(w[o], 0) / sum
	}
	return p
}

// SoftmaxLogprobs turns per-option log-probabilities (several tokens may map to the same
// option, e.g. "A" and " A") into a distribution renormalized over the valid options.
func SoftmaxLogprobs(logprobs map[string][]float64, options []string) map[string]float64 {
	maxLP := math.Inf(-1)
	for _, o := range options {
		for _, lp := range logprobs[o] {
			maxLP = max(maxLP, lp)
		}
	}
	if math.IsInf(maxLP, -1) {
		return nil
	}
	w := make(map[string]float64, len(options))
	for _, o := range options {
		for _, lp := range logprobs[o] {
			w[o] += math.Exp(lp - maxLP)
		}
	}
	return Normalize(w, options)
}

// AnswerFrom builds a typed answer from a distribution over q.Options().
func AnswerFrom(q Question, p map[string]float64, backend string) Answer {
	opts := q.Options()
	a := Answer{Name: q.Name, Type: q.Type, Probabilities: p, Backend: backend}
	a.Choice = argmax(p, opts)
	a.Confidence = Confidence(p, len(opts))
	switch q.Type {
	case Binary:
		a.Probability = p["true"]
	case Score:
		a.Probability = p[a.Choice]
		a.Score = ExpectedLevel(p, q.Levels)
	default:
		a.Probability = p[a.Choice]
	}
	return a
}

// OneHot puts all probability mass on one option.
func OneHot(q Question, option string) map[string]float64 {
	p := make(map[string]float64)
	for _, o := range q.Options() {
		p[o] = 0
	}
	p[option] = 1
	return p
}

// BinaryAnswer builds a binary answer from P(true).
func BinaryAnswer(q Question, pTrue float64, backend string) Answer {
	pTrue = clamp01(pTrue)
	return AnswerFrom(q, map[string]float64{"true": pTrue, "false": 1 - pTrue}, backend)
}

// ExpectedLevel is the probability-weighted 0-based level index.
func ExpectedLevel(p map[string]float64, levels []string) float64 {
	var s float64
	for i, l := range levels {
		s += float64(i) * p[l]
	}
	return s
}

// argmax picks the most likely option; ties go to the earlier option.
func argmax(p map[string]float64, opts []string) string {
	best, bestP := "", -1.0
	for _, o := range opts {
		if p[o] > bestP {
			best, bestP = o, p[o]
		}
	}
	return best
}

func clamp01(x float64) float64 { return min(max(x, 0), 1) }

func sortedKeys[V any](m map[string]V) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	slices.Sort(keys)
	return keys
}
