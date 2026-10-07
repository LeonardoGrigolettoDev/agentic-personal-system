// Package config reads the service configuration from the environment (see compose.yaml).
package config

import (
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"
)

type Config struct {
	Port              string
	DatabaseURL       string
	RedisAddr         string
	RedisPassword     string
	RedisDB           int
	APIKey            string
	Backends          []string
	Threshold         float64
	LocalMode         string
	RulesFile         string
	RequiredMigration string
	DecideTimeout     time.Duration
	HermesSecret      string
	LogLevel          string

	LiteLLMBaseURL string
	LiteLLMAPIKey  string
	OllamaBaseURL  string
	OllamaModel    string

	TypesafeAPIKey string
	JevModel       string
	JevBaseURL     string

	OpenAIAPIKey    string
	OpenAIModel     string
	OpenAIBaseURL   string
	UpstreamTimeout time.Duration
}

func Load() (Config, error) {
	c := Config{
		Port:              env("PORT", "8080"),
		DatabaseURL:       os.Getenv("DATABASE_URL"),
		RedisAddr:         os.Getenv("REDIS_ADDR"),
		RedisPassword:     os.Getenv("REDIS_PASSWORD"),
		APIKey:            os.Getenv("DECISION_API_KEY"),
		Backends:          list(env("DECISION_BACKENDS", "rules,local")),
		LocalMode:         env("DECISION_LOCAL_MODE", "logprobs"),
		RulesFile:         os.Getenv("DECISION_RULES_FILE"),
		RequiredMigration: env("DECISION_REQUIRED_MIGRATION", "005_costs_decisions"),
		HermesSecret:      os.Getenv("HERMES_WEBHOOK_SECRET"),
		LogLevel:          env("LOG_LEVEL", "info"),
		LiteLLMBaseURL:    env("LITELLM_BASE_URL", "http://litellm:4000"),
		LiteLLMAPIKey:     os.Getenv("LITELLM_API_KEY"),
		OllamaBaseURL:     env("OLLAMA_BASE_URL", "http://host.docker.internal:11434"),
		OllamaModel:       env("OLLAMA_DECIDER_MODEL", "qwen3:4b-instruct-2507-q4_K_M"),
		TypesafeAPIKey:    os.Getenv("TYPESAFE_API_KEY"),
		JevModel:          env("JEV_MODEL", "jev-latest"),
		JevBaseURL:        os.Getenv("JEV_BASE_URL"),
		OpenAIAPIKey:      os.Getenv("OPENAI_API_KEY"),
		OpenAIModel:       env("OPENAI_DECISIONS_MODEL", "gpt-6-luna"),
		OpenAIBaseURL:     os.Getenv("OPENAI_BASE_URL"),
	}
	var err error
	if c.RedisDB, err = strconv.Atoi(env("REDIS_DB", "0")); err != nil {
		return c, fmt.Errorf("REDIS_DB: %w", err)
	}
	if c.Threshold, err = strconv.ParseFloat(env("DECISION_THRESHOLD", "0.8"), 64); err != nil || c.Threshold < 0 || c.Threshold > 1 {
		return c, fmt.Errorf("DECISION_THRESHOLD must be a number in [0,1]")
	}
	if c.DecideTimeout, err = time.ParseDuration(env("DECISION_TIMEOUT", "90s")); err != nil {
		return c, fmt.Errorf("DECISION_TIMEOUT: %w", err)
	}
	if c.UpstreamTimeout, err = time.ParseDuration(env("DECISION_UPSTREAM_TIMEOUT", "30s")); err != nil {
		return c, fmt.Errorf("DECISION_UPSTREAM_TIMEOUT: %w", err)
	}
	if c.APIKey == "" {
		return c, fmt.Errorf("DECISION_API_KEY is required")
	}
	return c, nil
}

func env(key, def string) string {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		return v
	}
	return def
}

func list(s string) []string {
	var out []string
	for _, p := range strings.Split(s, ",") {
		if p = strings.TrimSpace(p); p != "" {
			out = append(out, p)
		}
	}
	return out
}
