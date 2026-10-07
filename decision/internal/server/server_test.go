package server

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"aios/decision/internal/decide"
	"aios/decision/internal/store"
)

type fakeEngine struct{ answers []decide.Answer }

func (f fakeEngine) DecideTrace(_ context.Context, req decide.Request) ([]decide.Answer, []decide.Step, error) {
	return f.answers, []decide.Step{{Backend: "rules", Asked: []string{"domain"}}}, nil
}
func (fakeEngine) Plan(decide.Request) ([]string, error) { return []string{"rules"}, nil }
func (fakeEngine) Threshold(decide.Request) float64      { return 0.8 }

type fakeStore struct {
	mu        sync.Mutex
	decisions []store.DecisionRow
	events    []string
}

func (s *fakeStore) Ready(context.Context, string) error { return nil }
func (s *fakeStore) InsertDecision(_ context.Context, r store.DecisionRow) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.decisions = append(s.decisions, r)
	return nil
}
func (s *fakeStore) InsertEvent(_ context.Context, _, typ string, _ json.RawMessage) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.events = append(s.events, typ)
	return nil
}

func newServer(st *fakeStore) *Server {
	return &Server{
		Engine: fakeEngine{answers: []decide.Answer{{Name: "domain", Type: decide.Choice, Choice: "engineering",
			Confidence: 1, Backend: "rules"}}},
		Store: st, APIKey: "secret-key", HermesSecret: "hook", DecideTimeout: time.Second,
		Log: slog.New(slog.NewTextHandler(io.Discard, nil)),
	}
}

const body = `{"state":"bug","questions":[{"name":"domain","type":"choice","criteria":{"engineering":"","finance":""}}]}`

func TestDecideRequiresBearer(t *testing.T) {
	h := newServer(&fakeStore{}).Handler()
	for _, auth := range []string{"", "Bearer wrong", "secret-key"} {
		req := httptest.NewRequest("POST", "/v1/decide", strings.NewReader(body))
		if auth != "" {
			req.Header.Set("Authorization", auth)
		}
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, req)
		if rec.Code != http.StatusUnauthorized {
			t.Errorf("auth %q: status %d", auth, rec.Code)
		}
	}
}

func TestDecidePersists(t *testing.T) {
	st := &fakeStore{}
	srv := newServer(st)
	req := httptest.NewRequest("POST", "/v1/decide", strings.NewReader(body))
	req.Header.Set("Authorization", "Bearer secret-key")
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)
	srv.Wait()
	if rec.Code != http.StatusOK {
		t.Fatalf("status %d: %s", rec.Code, rec.Body)
	}
	var resp decideResponse
	_ = json.Unmarshal(rec.Body.Bytes(), &resp)
	if resp.RequestID == "" || len(resp.Answers) != 1 || resp.NeedsHuman {
		t.Fatalf("resp %+v", resp)
	}
	if len(st.decisions) != 1 || st.decisions[0].Backend != "rules" {
		t.Fatalf("not persisted: %+v", st.decisions)
	}
}

func TestDecideRejectsInvalid(t *testing.T) {
	for _, b := range []string{`{`, `{"state":"x","questions":[]}`, `{"state":"x","questions":[],"bogus":1}`} {
		req := httptest.NewRequest("POST", "/v1/decide", strings.NewReader(b))
		req.Header.Set("Authorization", "Bearer secret-key")
		rec := httptest.NewRecorder()
		newServer(&fakeStore{}).Handler().ServeHTTP(rec, req)
		if rec.Code != http.StatusBadRequest {
			t.Errorf("%s: status %d", b, rec.Code)
		}
	}
}

func sign(secret, body string) string {
	m := hmac.New(sha256.New, []byte(secret))
	m.Write([]byte(body))
	return "sha256=" + hex.EncodeToString(m.Sum(nil))
}

func TestHermesEventsHMAC(t *testing.T) {
	st := &fakeStore{}
	h := newServer(st).Handler()
	payload := `{"event":"agent:end","session_id":"run-1"}`

	bad := httptest.NewRequest("POST", "/v1/hermes-events", strings.NewReader(payload))
	bad.Header.Set("X-Hermes-Signature-256", sign("wrong", payload))
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, bad)
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("bad signature accepted: %d", rec.Code)
	}

	good := httptest.NewRequest("POST", "/v1/hermes-events", strings.NewReader(payload))
	good.Header.Set("X-Hermes-Signature-256", sign("hook", payload))
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, good)
	if rec.Code != http.StatusAccepted || len(st.events) != 1 || st.events[0] != "agent:end" {
		t.Fatalf("status %d events %v", rec.Code, st.events)
	}
}

func TestHealthIsOpen(t *testing.T) {
	rec := httptest.NewRecorder()
	newServer(&fakeStore{}).Handler().ServeHTTP(rec, httptest.NewRequest("GET", "/healthz", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("status %d", rec.Code)
	}
}
