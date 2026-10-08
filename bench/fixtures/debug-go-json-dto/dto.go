// Package api expõe pedidos para o app mobile da Nitro.
package api

import "encoding/json"

// Pedido é o modelo interno.
type Pedido struct {
	ID         int
	Cliente    string
	ValorTotal float64
	Cancelado  bool
}

// PedidoDTO é o contrato JSON consumido pelo app: {"id", "cliente", "valor_total", "status"}.
// Todos os campos são obrigatórios no contrato, inclusive valor_total = 0 (pedido cortesia).
type PedidoDTO struct {
	ID         int     `json:"id"`
	cliente    string  `json:"cliente"`
	ValorTotal float64 `json:"valor_total,omitempty"`
	Status     string  `json:"status"`
}

func NovoDTO(p Pedido) PedidoDTO {
	status := "ativo"
	if p.Cancelado {
		status = "cancelado"
	}
	return PedidoDTO{ID: p.ID, cliente: p.Cliente, ValorTotal: p.ValorTotal, Status: status}
}

func Serializa(p Pedido) ([]byte, error) {
	return json.Marshal(NovoDTO(p))
}
