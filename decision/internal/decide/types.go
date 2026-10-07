// Package decide holds the backend-neutral decision schema shared by every engine.
// It mirrors the common shape of Jev (TypeSafe System One) and OpenAI Decisions,
// so each backend is only a mapper (docs/research-jev-decisions.md).
package decide

import (
	"context"
	"encoding/base64"
	"errors"
	"fmt"
	"net/http"
	"regexp"
	"strings"
)

type QType string

const (
	Binary QType = "binary"
	Choice QType = "choice"
	Score  QType = "score"
)

const (
	MaxQuestions = 64
	MaxOptions   = 255 // Jev's documented limit for choice
	MinLevels    = 2
	MaxLevels    = 10
	MaxImages    = 8
)

type Question struct {
	Name         string `json:"name"`
	Type         QType  `json:"type"`
	Instructions string `json:"instructions,omitempty"`
	// Criteria: choice -> option:description; binary -> optional "true"/"false" descriptions.
	Criteria map[string]string `json:"criteria,omitempty"`
	// Levels: score only, ordered from lowest to highest.
	Levels []string `json:"levels,omitempty"`
}

type Request struct {
	State     any        `json:"state"`
	Questions []Question `json:"questions"`
	Threshold float64    `json:"threshold,omitempty"`
	Backends  []string   `json:"backends,omitempty"`
	Images    []string   `json:"images,omitempty"` // data URLs or raw base64
	RunID     string     `json:"run_id,omitempty"`
	TaskID    string     `json:"task_id,omitempty"`
}

// Answer fields by type:
//   - binary: Choice "true"/"false", Probability = P(true).
//   - choice: Choice = option, Probability = P(choice).
//   - score:  Choice = level label, Score = probability-weighted level index (0-based),
//     Probability = P(Choice).
//
// Probabilities is keyed by option / level label / "true","false".
type Answer struct {
	Name          string             `json:"name"`
	Type          QType              `json:"type"`
	Choice        string             `json:"choice,omitempty"`
	Probability   float64            `json:"probability"`
	Score         float64            `json:"score"`
	Confidence    float64            `json:"confidence"`
	Probabilities map[string]float64 `json:"probabilities,omitempty"`
	Backend       string             `json:"backend"`
	Refused       bool               `json:"refused,omitempty"`
	NeedsHuman    bool               `json:"needs_human,omitempty"`
}

// DecisionEngine answers some or all of the questions in a request.
// Missing answers mean "could not decide"; the cascade forwards them to the next engine.
type DecisionEngine interface {
	Name() string
	Decide(ctx context.Context, req Request) ([]Answer, error)
}

var (
	nameRe = regexp.MustCompile(`^[A-Za-z][A-Za-z0-9_.-]{0,63}$`)
	uuidRe = regexp.MustCompile(`^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$`)
)

// ValidationError is returned for client mistakes (HTTP 400).
type ValidationError struct{ Msg string }

func (e *ValidationError) Error() string { return e.Msg }

func invalid(format string, a ...any) error { return &ValidationError{Msg: fmt.Sprintf(format, a...)} }

func IsValidation(err error) bool {
	var v *ValidationError
	return errors.As(err, &v)
}

// Validate checks the request and normalizes images to data URLs in place.
func (r *Request) Validate() error {
	if r.State == nil {
		return invalid("state is required")
	}
	if len(r.Questions) == 0 {
		return invalid("at least one question is required")
	}
	if len(r.Questions) > MaxQuestions {
		return invalid("too many questions (%d > %d)", len(r.Questions), MaxQuestions)
	}
	if r.Threshold < 0 || r.Threshold > 1 {
		return invalid("threshold must be within [0,1]")
	}
	seen := make(map[string]bool, len(r.Questions))
	for i := range r.Questions {
		q := &r.Questions[i]
		if !nameRe.MatchString(q.Name) {
			return invalid("question %d: name %q must match %s", i, q.Name, nameRe)
		}
		if seen[q.Name] {
			return invalid("duplicate question name %q", q.Name)
		}
		seen[q.Name] = true
		if err := q.validate(); err != nil {
			return err
		}
	}
	for _, id := range []string{r.RunID, r.TaskID} {
		if id != "" && !uuidRe.MatchString(id) {
			return invalid("run_id/task_id must be UUIDs")
		}
	}
	if len(r.Images) > MaxImages {
		return invalid("too many images (%d > %d)", len(r.Images), MaxImages)
	}
	for i, img := range r.Images {
		url, err := imageDataURL(img)
		if err != nil {
			return invalid("image %d: %v", i, err)
		}
		r.Images[i] = url
	}
	return nil
}

func (q *Question) validate() error {
	switch q.Type {
	case Binary:
		for k := range q.Criteria {
			if k != "true" && k != "false" {
				return invalid("question %q: binary criteria keys must be \"true\"/\"false\"", q.Name)
			}
		}
		if len(q.Levels) > 0 {
			return invalid("question %q: levels are only valid for score", q.Name)
		}
	case Choice:
		if n := len(q.Criteria); n < 2 || n > MaxOptions {
			return invalid("question %q: choice needs 2..%d options, got %d", q.Name, MaxOptions, n)
		}
		for k := range q.Criteria {
			if strings.TrimSpace(k) == "" {
				return invalid("question %q: empty option", q.Name)
			}
		}
		if len(q.Levels) > 0 {
			return invalid("question %q: levels are only valid for score", q.Name)
		}
	case Score:
		if n := len(q.Levels); n < MinLevels || n > MaxLevels {
			return invalid("question %q: score needs %d..%d levels, got %d", q.Name, MinLevels, MaxLevels, n)
		}
		seen := map[string]bool{}
		for _, l := range q.Levels {
			if strings.TrimSpace(l) == "" || seen[l] {
				return invalid("question %q: levels must be non-empty and unique", q.Name)
			}
			seen[l] = true
		}
		if len(q.Criteria) > 0 {
			return invalid("question %q: score uses levels, not criteria", q.Name)
		}
	default:
		return invalid("question %q: type must be binary|choice|score", q.Name)
	}
	return nil
}

// Options returns the valid answer labels for a question, in a stable order
// (binary: true,false; choice: sorted keys; score: levels as given).
func (q Question) Options() []string {
	switch q.Type {
	case Binary:
		return []string{"true", "false"}
	case Choice:
		return sortedKeys(q.Criteria)
	case Score:
		return q.Levels
	}
	return nil
}

func imageDataURL(s string) (string, error) {
	if strings.HasPrefix(s, "data:image/") {
		if !strings.Contains(s, ";base64,") {
			return "", errors.New("data URL must be base64")
		}
		return s, nil
	}
	if strings.HasPrefix(s, "http://") || strings.HasPrefix(s, "https://") {
		return "", errors.New("remote URLs are not accepted; send a data URL or base64")
	}
	head := s
	if len(head) > 1024 {
		head = head[:1024]
	}
	raw, err := base64.StdEncoding.DecodeString(head[:len(head)/4*4])
	if err != nil {
		return "", errors.New("not a data URL or valid base64")
	}
	mime := http.DetectContentType(raw)
	if !strings.HasPrefix(mime, "image/") {
		return "", fmt.Errorf("unsupported content type %s", mime)
	}
	return "data:" + mime + ";base64," + s, nil
}
