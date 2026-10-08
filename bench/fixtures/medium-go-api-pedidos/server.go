package api

import "net/http"

// NovoServidor monta as rotas da API de pedidos do mini-ERP (net/http puro, Go 1.22+ ServeMux):
//
//	GET  /healthz          -> 204 sem corpo
//	POST /v1/pedidos       -> cria a partir de {"cliente": "...", "total": 12.5}; 201 + JSON do pedido criado.
//	                          JSON inválido, cliente vazio ou total <= 0 -> 400 com {"erro": "..."}
//	GET  /v1/pedidos/{id}  -> 200 + JSON do pedido; id não numérico -> 400 {"erro"}; inexistente -> 404 {"erro"}
//
// Todas as respostas com corpo usam Content-Type: application/json.
func NovoServidor(s *Store) http.Handler {
	return http.NotFoundHandler() // TODO
}
