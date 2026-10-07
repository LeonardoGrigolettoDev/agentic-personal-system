package notify

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"mime"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
)

func TestTelegramAndNtfy(t *testing.T) {
	var mu sync.Mutex
	got := map[string]string{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		mu.Lock()
		got[r.URL.Path] = string(b) + "|" + r.Header.Get("Title") + "|" + r.Header.Get("Priority")
		mu.Unlock()
	}))
	defer srv.Close()

	f := &Fanout{Log: slog.New(slog.NewTextHandler(io.Discard, nil)), Targets: []Notifier{
		Telegram{Token: "T0K", ChatID: "42", BaseURL: srv.URL},
		Ntfy{URL: srv.URL + "/aios-topic"},
	}}
	f.Send("Aprovação necessária", "tier6-opus para a sessão s1", true)
	f.Wait()

	var msg map[string]any
	_ = json.Unmarshal([]byte(strings.Split(got["/botT0K/sendMessage"], "|")[0]), &msg)
	if msg["chat_id"] != "42" || !strings.Contains(msg["text"].(string), "tier6-opus") {
		t.Fatalf("telegram payload: %v", got)
	}
	parts := strings.Split(got["/aios-topic"], "|")
	title, _ := new(mime.WordDecoder).DecodeHeader(parts[1])
	if parts[0] != "tier6-opus para a sessão s1" || title != "Aprovação necessária" || parts[2] != "high" {
		t.Fatalf("ntfy payload: %q (title %q)", got["/aios-topic"], title)
	}
}

func TestFromEnvAndDisabled(t *testing.T) {
	env := map[string]string{"NTFY_URL": "https://ntfy.sh/x", "TELEGRAM_BOT_TOKEN": "t"}
	f := FromEnv(func(k string) string { return env[k] }, slog.New(slog.NewTextHandler(io.Discard, nil)))
	if len(f.Targets) != 1 { // telegram needs both token and chat id
		t.Fatalf("targets %d", len(f.Targets))
	}
	var none *Fanout
	none.Send("x", "y", false) // nil-safe
	if none.Enabled() {
		t.Fatal("nil fanout enabled")
	}
}

func TestRedactsToken(t *testing.T) {
	err := redact(errors.New(`Post "https://api.telegram.org/botSECRET/sendMessage": dial tcp: timeout`),
		"https://api.telegram.org/botSECRET/sendMessage")
	if strings.Contains(err.Error(), "SECRET") {
		t.Fatalf("token leaked: %v", err)
	}
	_ = context.Background()
}
