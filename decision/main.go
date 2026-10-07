// Command decision is the AIOS Decision Service: typed questions over a state, answered by a
// cascade of backends (rules -> local Qwen -> Jev / OpenAI Decisions), with calibrated confidence.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"aios/decision/internal/config"
	"aios/decision/internal/decide"
	"aios/decision/internal/jev"
	"aios/decision/internal/ledger"
	"aios/decision/internal/local"
	"aios/decision/internal/openai"
	"aios/decision/internal/policy"
	"aios/decision/internal/rules"
	"aios/decision/internal/server"
	"aios/decision/internal/store"
)

func main() {
	healthcheck := flag.Bool("healthcheck", false, "GET /healthz on the local port and exit 0/1 (container healthcheck)")
	flag.Parse()
	if *healthcheck {
		os.Exit(probe(os.Getenv("PORT")))
	}
	if err := run(); err != nil {
		slog.Error("fatal", "err", err)
		os.Exit(1)
	}
}

func probe(port string) int {
	if port == "" {
		port = "8080"
	}
	client := http.Client{Timeout: 3 * time.Second}
	resp, err := client.Get("http://" + net.JoinHostPort("127.0.0.1", port) + "/healthz")
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return 1
	}
	return 0
}

func run() error {
	cfg, err := config.Load()
	if err != nil {
		return err
	}
	log := newLogger(cfg.LogLevel)
	slog.SetDefault(log)

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	engines, err := buildEngines(cfg, log)
	if err != nil {
		return err
	}
	cascade := decide.NewCascade(engines, cfg.Backends, cfg.Threshold, log)
	log.Info("decision cascade", "order", cascade.Order(), "threshold", cfg.Threshold, "local_mode", cfg.LocalMode)

	srv := &server.Server{
		Engine:            cascade,
		APIKey:            cfg.APIKey,
		HermesSecret:      cfg.HermesSecret,
		RequiredMigration: cfg.RequiredMigration,
		DecideTimeout:     cfg.DecideTimeout,
		Log:               log,
	}
	pol, err := policy.Load(cfg.PolicyFile, cfg.AgentsDir)
	if err != nil {
		return fmt.Errorf("policy: %w", err)
	}
	srv.Policy = pol
	log.Info("policy loaded", "agents", len(pol.Agents), "task_types", len(pol.TaskTypes), "default_model", pol.DefaultModel)

	if cfg.DatabaseURL != "" {
		db, err := store.Open(ctx, cfg.DatabaseURL)
		if err != nil {
			return err
		}
		defer db.Close()
		srv.Store = db
		led := ledger.New(db.Pool(), cfg.BudgetTZ)
		srv.Ledger = led
		go syncAgents(ctx, led, pol, log)
	} else {
		log.Warn("DATABASE_URL not set: decisions are not persisted")
	}
	if cfg.RedisAddr != "" {
		cache := store.NewCache(cfg.RedisAddr, cfg.RedisPassword, cfg.RedisDB)
		defer cache.Close()
		srv.Cache = cache
	}
	if cfg.HermesSecret == "" {
		log.Warn("HERMES_WEBHOOK_SECRET not set: /v1/hermes-events is disabled")
	}

	httpSrv := &http.Server{
		Addr:              ":" + cfg.Port,
		Handler:           srv.Handler(),
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       30 * time.Second,
		WriteTimeout:      cfg.DecideTimeout + 10*time.Second,
		IdleTimeout:       60 * time.Second,
		MaxHeaderBytes:    32 << 10,
	}
	errc := make(chan error, 1)
	go func() {
		log.Info("listening", "addr", httpSrv.Addr)
		errc <- httpSrv.ListenAndServe()
	}()

	select {
	case err := <-errc:
		if !errors.Is(err, http.ErrServerClosed) {
			return err
		}
	case <-ctx.Done():
		log.Info("shutting down")
	}
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	err = httpSrv.Shutdown(shutdownCtx)
	srv.Wait()
	return err
}

// syncAgents mirrors agents/*/agent.yaml into the DB, retrying while Postgres comes up.
func syncAgents(ctx context.Context, led *ledger.DB, pol *policy.Policy, log *slog.Logger) {
	for attempt := 1; ; attempt++ {
		err := led.SyncAgents(ctx, pol.Agents)
		if err == nil {
			log.Info("agents synced", "count", len(pol.Agents))
			return
		}
		if attempt >= 30 || ctx.Err() != nil {
			log.Error("agent sync gave up", "err", err)
			return
		}
		log.Warn("agent sync failed, retrying", "attempt", attempt, "err", err)
		select {
		case <-ctx.Done():
			return
		case <-time.After(5 * time.Second):
		}
	}
}

// buildEngines registers every backend that has what it needs; the rest are skipped.
func buildEngines(cfg config.Config, log *slog.Logger) ([]decide.DecisionEngine, error) {
	var engines []decide.DecisionEngine
	if cfg.RulesFile != "" {
		r, err := rules.Load(cfg.RulesFile, log)
		if err != nil {
			return nil, fmt.Errorf("DECISION_RULES_FILE: %w", err)
		}
		log.Info("backend registered", "backend", "rules", "rules", r.Len())
		engines = append(engines, r)
	}
	if cfg.LocalMode == local.ModeVote && cfg.LiteLLMAPIKey == "" {
		log.Warn("backend skipped: vote mode needs LITELLM_API_KEY", "backend", "local")
	} else {
		l, err := local.New(local.Config{
			Mode: cfg.LocalMode, OllamaBaseURL: cfg.OllamaBaseURL, Model: cfg.OllamaModel,
			LiteLLMURL: cfg.LiteLLMBaseURL, LiteLLMKey: cfg.LiteLLMAPIKey, Timeout: cfg.DecideTimeout,
		})
		if err != nil {
			return nil, err
		}
		log.Info("backend registered", "backend", "local", "mode", cfg.LocalMode)
		engines = append(engines, l)
	}
	if cfg.TypesafeAPIKey != "" {
		engines = append(engines, jev.New(cfg.JevBaseURL, cfg.TypesafeAPIKey, cfg.JevModel, cfg.UpstreamTimeout))
		log.Info("backend registered", "backend", "jev", "model", cfg.JevModel)
	}
	if cfg.OpenAIAPIKey != "" {
		engines = append(engines, openai.New(cfg.OpenAIBaseURL, cfg.OpenAIAPIKey, cfg.OpenAIModel, cfg.UpstreamTimeout))
		log.Info("backend registered", "backend", "openai", "model", cfg.OpenAIModel)
	}
	return engines, nil
}

func newLogger(level string) *slog.Logger {
	var lv slog.Level
	if err := lv.UnmarshalText([]byte(level)); err != nil {
		lv = slog.LevelInfo
	}
	return slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: lv}))
}
