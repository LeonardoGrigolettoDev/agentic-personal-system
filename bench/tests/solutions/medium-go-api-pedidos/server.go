package api

import (
	"encoding/json"
	"errors"
	"net/http"
	"strconv"
	"strings"
)

func NovoServidor(s *Store) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusNoContent) })
	mux.HandleFunc("POST /v1/pedidos", func(w http.ResponseWriter, r *http.Request) {
		var p Pedido
		if err := json.NewDecoder(r.Body).Decode(&p); err != nil {
			erro(w, http.StatusBadRequest, "JSON inválido")
			return
		}
		if strings.TrimSpace(p.Cliente) == "" || p.Total <= 0 {
			erro(w, http.StatusBadRequest, "cliente e total > 0 são obrigatórios")
			return
		}
		responde(w, http.StatusCreated, s.Cria(p))
	})
	mux.HandleFunc("GET /v1/pedidos/{id}", func(w http.ResponseWriter, r *http.Request) {
		id, err := strconv.Atoi(r.PathValue("id"))
		if err != nil {
			erro(w, http.StatusBadRequest, "id inválido")
			return
		}
		p, err := s.Busca(id)
		if errors.Is(err, ErrNaoEncontrado) {
			erro(w, http.StatusNotFound, "pedido não encontrado")
			return
		}
		responde(w, http.StatusOK, p)
	})
	return mux
}

func responde(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func erro(w http.ResponseWriter, code int, msg string) {
	responde(w, code, map[string]string{"erro": msg})
}
