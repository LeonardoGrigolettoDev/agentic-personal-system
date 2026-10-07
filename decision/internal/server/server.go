// Package server exposes the decision cascade over HTTP.
package server

import (
	"context"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"sync"
	"time"

	"aios/decision/internal/decide"
	"aios/decision/internal/policy"
	"aios/decision/internal/store"
)

const (
	maxDecideBody  = 10 << 20 // room for a few base64 images
	maxEventBody   = 1 << 20
	persistTimeout = 5 * time.Second
)

type Decider interface {
	DecideTrace(ctx context.Context, req decide.Request) ([]decide.Answer, []decide.Step, error)
	Plan(req decide.Request) ([]string, error)
	Threshold(req decide.Request) float64
}

type Store interface {
	Ready(ctx context.Context, migration string) error
	InsertDecision(ctx context.Context, r store.DecisionRow) error
	InsertEvent(ctx context.Context, source, typ string, payload json.RawMessage) error
}

type Cache interface {
	Get(ctx context.Context, key string) ([]decide.Answer, error)
	Set(ctx context.Context, key string, answers []decide.Answer) error
}

type Server struct {
	Engine            Decider
	Store             Store // nil: no persistence, /readyz fails
	Cache             Cache // nil: no caching
	Policy            *policy.Policy
	Ledger            Ledger // nil: routing/budget/escalation endpoints answer 503
	APIKey            string
	HermesSecret      string
	RequiredMigration string
	DecideTimeout     time.Duration
	Log               *slog.Logger

	bg sync.WaitGroup // background persistence
}

func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
	mux.HandleFunc("GET /readyz", s.readyz)
	mux.Handle("POST /v1/decide", RequireBearer(s.APIKey, http.HandlerFunc(s.decide)))
	mux.HandleFunc("POST /v1/hermes-events", s.hermesEvent)
	s.policyRoutes(mux)
	return s.logRequests(mux)
}

// Wait blocks until background writes finish (call on shutdown).
func (s *Server) Wait() { s.bg.Wait() }

func (s *Server) readyz(w http.ResponseWriter, r *http.Request) {
	if s.Store == nil {
		writeError(w, http.StatusServiceUnavailable, "database not configured")
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
	defer cancel()
	if err := s.Store.Ready(ctx, s.RequiredMigration); err != nil {
		writeError(w, http.StatusServiceUnavailable, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "ready"})
}

type decideResponse struct {
	RequestID    string          `json:"request_id"`
	Answers      []decide.Answer `json:"answers"`
	NeedsHuman   bool            `json:"needs_human"`
	Cached       bool            `json:"cached"`
	LatencyMS    int64           `json:"latency_ms"`
	BackendTrace []decide.Step   `json:"backend_trace"`
}

func (s *Server) decide(w http.ResponseWriter, r *http.Request) {
	start := time.Now()
	var req decide.Request
	dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, maxDecideBody))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
		return
	}
	if err := req.Validate(); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	if _, err := s.Engine.Plan(req); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), s.DecideTimeout)
	defer cancel()
	ctx, usage := decide.WithUsage(ctx)
	resp := decideResponse{RequestID: newID()}
	key := decide.CacheKey(req, s.Engine.Threshold(req))

	if s.Cache != nil {
		if cached, err := s.Cache.Get(ctx, key); err != nil {
			s.Log.WarnContext(ctx, "cache get failed", "err", err)
		} else if cached != nil {
			resp.Answers, resp.Cached = cached, true
			resp.BackendTrace = []decide.Step{{Backend: "cache", Asked: questionNames(req)}}
		}
	}
	if !resp.Cached {
		answers, trace, err := s.Engine.DecideTrace(ctx, req)
		if err != nil {
			status := http.StatusInternalServerError
			if decide.IsValidation(err) {
				status = http.StatusBadRequest
			}
			writeError(w, status, err.Error())
			return
		}
		resp.Answers, resp.BackendTrace = answers, trace
	}

	backend, minConf, needsHuman := decide.Summary(resp.Answers)
	if resp.Cached {
		backend = "cache"
	}
	resp.NeedsHuman = needsHuman
	resp.LatencyMS = time.Since(start).Milliseconds()

	if s.Cache != nil && !resp.Cached && !needsHuman && !anyFailed(resp.BackendTrace) {
		if err := s.Cache.Set(ctx, key, resp.Answers); err != nil {
			s.Log.WarnContext(ctx, "cache set failed", "err", err)
		}
	}
	tokens, cost := usage.Snapshot()
	s.persist(store.DecisionRow{
		RequestID: resp.RequestID, RequestHash: decide.RequestHash(req), Backend: backend,
		State: req.State, Questions: req.Questions, Answers: resp.Answers,
		MinConfidence: minConf, NeedsHuman: needsHuman, LatencyMS: resp.LatencyMS,
		InputTokens: tokens, CostUSD: cost, RunID: req.RunID, TaskID: req.TaskID,
	})
	s.Log.InfoContext(ctx, "decision", "request_id", resp.RequestID, "backend", backend,
		"min_confidence", minConf, "needs_human", needsHuman, "latency_ms", resp.LatencyMS, "cached", resp.Cached)
	writeJSON(w, http.StatusOK, resp)
}

// persist writes the decision log in the background; a DB outage never blocks an answer.
func (s *Server) persist(row store.DecisionRow) {
	if s.Store == nil {
		return
	}
	s.bg.Go(func() {
		ctx, cancel := context.WithTimeout(context.Background(), persistTimeout)
		defer cancel()
		if err := s.Store.InsertDecision(ctx, row); err != nil {
			s.Log.Error("persist decision failed", "request_id", row.RequestID, "err", err)
		}
	})
}

func (s *Server) hermesEvent(w http.ResponseWriter, r *http.Request) {
	if s.HermesSecret == "" {
		writeError(w, http.StatusServiceUnavailable, "webhook secret not configured")
		return
	}
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, maxEventBody))
	if err != nil {
		writeError(w, http.StatusRequestEntityTooLarge, "body too large")
		return
	}
	if !VerifySignature(s.HermesSecret, body, r.Header.Get("X-Hermes-Signature-256")) {
		writeError(w, http.StatusUnauthorized, "invalid signature")
		return
	}
	var payload map[string]any
	if err := json.Unmarshal(body, &payload); err != nil {
		writeError(w, http.StatusBadRequest, "payload must be a JSON object")
		return
	}
	typ := "unknown"
	for _, k := range []string{"event", "type"} {
		if v, ok := payload[k].(string); ok && v != "" {
			typ = v
			break
		}
	}
	if s.Store == nil {
		writeError(w, http.StatusServiceUnavailable, "database not configured")
		return
	}
	if err := s.Store.InsertEvent(r.Context(), "hermes", typ, body); err != nil {
		s.Log.ErrorContext(r.Context(), "insert event failed", "type", typ, "err", err)
		writeError(w, http.StatusServiceUnavailable, "could not store event")
		return
	}
	writeJSON(w, http.StatusAccepted, map[string]string{"status": "accepted", "type": typ})
}

// VerifySignature checks a hex HMAC-SHA256 of body, optionally prefixed with "sha256=".
func VerifySignature(secret string, body []byte, header string) bool {
	got, err := hex.DecodeString(strings.TrimPrefix(strings.TrimSpace(header), "sha256="))
	if err != nil || len(got) != sha256.Size {
		return false
	}
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write(body)
	return hmac.Equal(got, mac.Sum(nil))
}

// RequireBearer rejects requests whose bearer token differs from key (constant time;
// hashing first also hides the key length).
func RequireBearer(key string, next http.Handler) http.Handler {
	want := sha256.Sum256([]byte(key))
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		token, ok := strings.CutPrefix(r.Header.Get("Authorization"), "Bearer ")
		got := sha256.Sum256([]byte(token))
		if !ok || key == "" || subtle.ConstantTimeCompare(got[:], want[:]) != 1 {
			w.Header().Set("WWW-Authenticate", `Bearer realm="decision"`)
			writeError(w, http.StatusUnauthorized, "unauthorized")
			return
		}
		next.ServeHTTP(w, r)
	})
}

type statusRecorder struct {
	http.ResponseWriter
	status int
}

func (r *statusRecorder) WriteHeader(code int) {
	r.status = code
	r.ResponseWriter.WriteHeader(code)
}

func (s *Server) logRequests(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		rec := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(rec, r)
		level := slog.LevelInfo
		if r.URL.Path == "/healthz" || r.URL.Path == "/readyz" {
			level = slog.LevelDebug
		}
		s.Log.Log(r.Context(), level, "http", "method", r.Method, "path", r.URL.Path,
			"status", rec.status, "duration_ms", time.Since(start).Milliseconds())
	})
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}

func anyFailed(trace []decide.Step) bool {
	for _, s := range trace {
		if s.Error != "" {
			return true
		}
	}
	return false
}

func questionNames(req decide.Request) []string {
	out := make([]string, len(req.Questions))
	for i, q := range req.Questions {
		out[i] = q.Name
	}
	return out
}

// newID returns a random RFC 4122 version-4 UUID.
func newID() string {
	var b [16]byte
	_, _ = rand.Read(b[:])
	b[6] = b[6]&0x0f | 0x40
	b[8] = b[8]&0x3f | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:])
}
