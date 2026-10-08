package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func chama(t *testing.T, h http.Handler, metodo, url, corpo string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(metodo, url, strings.NewReader(corpo))
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

func TestHealthz(t *testing.T) {
	if rec := chama(t, NovoServidor(NovoStore()), "GET", "/healthz", ""); rec.Code != 204 {
		t.Fatalf("healthz = %d", rec.Code)
	}
}

func TestCriaEBusca(t *testing.T) {
	h := NovoServidor(NovoStore())
	rec := chama(t, h, "POST", "/v1/pedidos", `{"cliente":"Ana","total":99.9}`)
	if rec.Code != 201 || !strings.HasPrefix(rec.Header().Get("Content-Type"), "application/json") {
		t.Fatalf("POST = %d %q", rec.Code, rec.Header().Get("Content-Type"))
	}
	var criado Pedido
	if err := json.Unmarshal(rec.Body.Bytes(), &criado); err != nil || criado.ID != 1 || criado.Cliente != "Ana" {
		t.Fatalf("criado = %+v (%v)", criado, err)
	}
	rec = chama(t, h, "GET", "/v1/pedidos/1", "")
	var lido Pedido
	if rec.Code != 200 || json.Unmarshal(rec.Body.Bytes(), &lido) != nil || lido != criado {
		t.Fatalf("GET = %d %s", rec.Code, rec.Body.String())
	}
}

func TestErros(t *testing.T) {
	h := NovoServidor(NovoStore())
	casos := []struct {
		metodo, url, corpo string
		code               int
	}{
		{"POST", "/v1/pedidos", `{"cliente":"","total":10}`, 400},
		{"POST", "/v1/pedidos", `{"cliente":"Bia","total":0}`, 400},
		{"POST", "/v1/pedidos", `{nao e json`, 400},
		{"GET", "/v1/pedidos/abc", "", 400},
		{"GET", "/v1/pedidos/42", "", 404},
		{"DELETE", "/v1/pedidos/1", "", 405},
	}
	for _, c := range casos {
		rec := chama(t, h, c.metodo, c.url, c.corpo)
		if rec.Code != c.code {
			t.Errorf("%s %s = %d, want %d", c.metodo, c.url, rec.Code, c.code)
			continue
		}
		if c.code == 400 || c.code == 404 {
			var e map[string]string
			if json.Unmarshal(rec.Body.Bytes(), &e) != nil || e["erro"] == "" {
				t.Errorf("%s %s: corpo de erro inválido %q", c.metodo, c.url, rec.Body.String())
			}
		}
	}
}
