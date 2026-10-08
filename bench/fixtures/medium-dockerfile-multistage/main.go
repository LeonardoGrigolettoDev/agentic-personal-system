// status: microserviço de health/status do app de entregas da Nitro.
package main

import (
	"encoding/json"
	"log"
	"net/http"
	"os"
	"time"
)

var inicio = time.Now()

func main() {
	addr := ":" + envOr("PORT", "8080")
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	})
	mux.HandleFunc("GET /status", func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{"uptime_s": int(time.Since(inicio).Seconds()), "versao": versao})
	})
	log.Printf("ouvindo em %s", addr)
	log.Fatal(http.ListenAndServe(addr, mux))
}

var versao = "dev"

func envOr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}
