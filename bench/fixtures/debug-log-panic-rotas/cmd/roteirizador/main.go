package main

import (
	"encoding/json"
	"log"
	"net/http"
	"strconv"

	"nitro.dev/roteirizador/internal/entrega"
)

var rotas = map[int]*entrega.Rota{}

func proxima(w http.ResponseWriter, r *http.Request) {
	id, _ := strconv.Atoi(r.PathValue("motoboy"))
	atual, _ := strconv.Atoi(r.URL.Query().Get("atual"))
	rota, ok := rotas[id]
	if !ok {
		http.NotFound(w, r)
		return
	}
	p, err := rota.ProximaParada(atual)
	if err != nil {
		w.WriteHeader(http.StatusNoContent)
		return
	}
	_ = json.NewEncoder(w).Encode(p)
}

func main() {
	http.HandleFunc("GET /v1/motoboys/{motoboy}/proxima", proxima)
	log.Fatal(http.ListenAndServe(":8080", nil))
}
