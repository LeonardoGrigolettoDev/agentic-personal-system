// Package notify pushes escalation/approval notices to the user (docs/ARCHITECTURE.md §8 "notificar usuário").
// Delivery is best-effort and asynchronous: a failed push is logged, never surfaced to the caller.
package notify

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"mime"
	"net/http"
	"strings"
	"sync"
	"time"
)

type Notifier interface {
	Notify(ctx context.Context, title, body string, urgent bool) error
}

// Telegram sends via the Bot API (sendMessage).
type Telegram struct {
	Token, ChatID string
	BaseURL       string // tests
	HTTP          *http.Client
}

func (t Telegram) Notify(ctx context.Context, title, body string, _ bool) error {
	base := t.BaseURL
	if base == "" {
		base = "https://api.telegram.org"
	}
	payload, _ := json.Marshal(map[string]any{"chat_id": t.ChatID, "text": "🔔 " + title + "\n\n" + body,
		"disable_web_page_preview": true})
	return post(ctx, t.HTTP, base+"/bot"+t.Token+"/sendMessage", payload, nil)
}

// Ntfy publishes to an ntfy topic URL (https://ntfy.sh/<topic> or self-hosted).
type Ntfy struct {
	URL  string
	HTTP *http.Client
}

func (n Ntfy) Notify(ctx context.Context, title, body string, urgent bool) error {
	prio := "default"
	if urgent {
		prio = "high"
	}
	// headers must stay ASCII: RFC 2047 encoding for accented titles (ntfy decodes it)
	return post(ctx, n.HTTP, n.URL, []byte(body), map[string]string{"Title": mime.QEncoding.Encode("utf-8", title),
		"Priority": prio, "Tags": "robot"})
}

func post(ctx context.Context, c *http.Client, url string, body []byte, headers map[string]string) error {
	if c == nil {
		c = &http.Client{Timeout: 10 * time.Second}
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return err
	}
	if headers == nil {
		req.Header.Set("Content-Type", "application/json")
	}
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	resp, err := c.Do(req)
	if err != nil {
		return redact(err, url)
	}
	resp.Body.Close()
	if resp.StatusCode/100 != 2 {
		return fmt.Errorf("notify: HTTP %d", resp.StatusCode)
	}
	return nil
}

// redact keeps bot tokens (part of the Telegram URL) out of logs.
func redact(err error, url string) error {
	msg := err.Error()
	if i := strings.Index(url, "/bot"); i >= 0 {
		msg = strings.ReplaceAll(msg, url[i:], "/bot<redacted>/…")
	}
	return fmt.Errorf("notify: %s", msg)
}

// Fanout delivers to every configured channel in the background.
type Fanout struct {
	Targets []Notifier
	Log     *slog.Logger
	wg      sync.WaitGroup
}

func (f *Fanout) Enabled() bool { return f != nil && len(f.Targets) > 0 }

func (f *Fanout) Send(title, body string, urgent bool) {
	if !f.Enabled() {
		return
	}
	for _, t := range f.Targets {
		f.wg.Go(func() {
			ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
			defer cancel()
			if err := t.Notify(ctx, title, body, urgent); err != nil {
				f.Log.Warn("notification failed", "err", err)
			}
		})
	}
}

// Wait blocks until queued notifications finish (shutdown, tests).
func (f *Fanout) Wait() {
	if f != nil {
		f.wg.Wait()
	}
}

// FromEnv builds the fan-out from TELEGRAM_BOT_TOKEN+TELEGRAM_CHAT_ID and NTFY_URL.
func FromEnv(getenv func(string) string, log *slog.Logger) *Fanout {
	f := &Fanout{Log: log}
	if tok, chat := getenv("TELEGRAM_BOT_TOKEN"), getenv("TELEGRAM_CHAT_ID"); tok != "" && chat != "" {
		f.Targets = append(f.Targets, Telegram{Token: tok, ChatID: chat})
	}
	if u := getenv("NTFY_URL"); u != "" {
		f.Targets = append(f.Targets, Ntfy{URL: u})
	}
	return f
}
