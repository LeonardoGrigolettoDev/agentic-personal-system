// Package httpx posts JSON to upstream APIs with bearer auth and bounded retries.
package httpx

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"slices"
	"strconv"
	"time"
)

const (
	maxResponse   = 8 << 20
	maxRetryAfter = 10 * time.Second
)

type Client struct {
	HTTP       *http.Client
	Token      string
	MaxRetries int           // retries after the first attempt
	Backoff    time.Duration // base delay, doubled per retry
	RetryOn    []int         // status codes worth retrying
}

// StatusError is a non-2xx upstream response.
type StatusError struct {
	Status int
	Body   string
}

func (e *StatusError) Error() string { return fmt.Sprintf("upstream status %d: %s", e.Status, e.Body) }

// PostJSON marshals in, posts it and decodes a 2xx response into out.
// Retryable statuses back off exponentially (base * 2^n), honoring Retry-After seconds
// (capped at 10s) when larger.
func (c *Client) PostJSON(ctx context.Context, url string, in, out any) error {
	body, err := json.Marshal(in)
	if err != nil {
		return err
	}
	for attempt := 0; ; attempt++ {
		retryAfter, err := c.do(ctx, url, body, out)
		var se *StatusError
		if err == nil || attempt >= c.MaxRetries || !errors.As(err, &se) || !slices.Contains(c.RetryOn, se.Status) {
			return err
		}
		delay := max(c.Backoff<<attempt, min(retryAfter, maxRetryAfter))
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(delay):
		}
	}
}

func (c *Client) do(ctx context.Context, url string, body []byte, out any) (time.Duration, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return 0, err
	}
	req.Header.Set("Content-Type", "application/json")
	if c.Token != "" {
		req.Header.Set("Authorization", "Bearer "+c.Token)
	}
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return 0, err
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, maxResponse))
	if err != nil {
		return 0, err
	}
	if resp.StatusCode/100 != 2 {
		var ra time.Duration
		if s, err := strconv.Atoi(resp.Header.Get("Retry-After")); err == nil {
			ra = time.Duration(s) * time.Second
		}
		return ra, &StatusError{Status: resp.StatusCode, Body: truncate(string(raw), 300)}
	}
	if err := json.Unmarshal(raw, out); err != nil {
		return 0, fmt.Errorf("decode response: %w", err)
	}
	return 0, nil
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "..."
}
