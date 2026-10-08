// Package vendas processa pedidos do mini-ERP.
package vendas

import (
	"errors"
	"fmt"
	"strings"
)

type Endereco struct {
	Rua, Numero, Cidade, UF, CEP string
}

type Item struct {
	SKU        string
	Quantidade int
	PrecoCent  int64
}

type Pedido struct {
	Cliente   string
	Entrega   Endereco
	Cobranca  Endereco
	Itens     []Item
	Cupom     string
	TotalCent int64
	FreteCent int64
}

var ErrPedidoInvalido = errors.New("pedido inválido")

// ProcessarPedido valida o pedido, calcula total, desconto e frete. Faz tudo num lugar só (e cresceu demais).
func ProcessarPedido(p *Pedido) error {
	if strings.TrimSpace(p.Cliente) == "" {
		return fmt.Errorf("%w: cliente vazio", ErrPedidoInvalido)
	}
	// valida endereço de entrega
	if strings.TrimSpace(p.Entrega.Rua) == "" || strings.TrimSpace(p.Entrega.Numero) == "" {
		return fmt.Errorf("%w: entrega: rua e número obrigatórios", ErrPedidoInvalido)
	}
	if len(p.Entrega.UF) != 2 {
		return fmt.Errorf("%w: entrega: UF inválida", ErrPedidoInvalido)
	}
	cepEntrega := strings.ReplaceAll(p.Entrega.CEP, "-", "")
	if len(cepEntrega) != 8 {
		return fmt.Errorf("%w: entrega: CEP inválido", ErrPedidoInvalido)
	}
	for _, c := range cepEntrega {
		if c < '0' || c > '9' {
			return fmt.Errorf("%w: entrega: CEP inválido", ErrPedidoInvalido)
		}
	}
	// valida endereço de cobrança
	if strings.TrimSpace(p.Cobranca.Rua) == "" || strings.TrimSpace(p.Cobranca.Numero) == "" {
		return fmt.Errorf("%w: cobrança: rua e número obrigatórios", ErrPedidoInvalido)
	}
	if len(p.Cobranca.UF) != 2 {
		return fmt.Errorf("%w: cobrança: UF inválida", ErrPedidoInvalido)
	}
	cepCobranca := strings.ReplaceAll(p.Cobranca.CEP, "-", "")
	if len(cepCobranca) != 8 {
		return fmt.Errorf("%w: cobrança: CEP inválido", ErrPedidoInvalido)
	}
	for _, c := range cepCobranca {
		if c < '0' || c > '9' {
			return fmt.Errorf("%w: cobrança: CEP inválido", ErrPedidoInvalido)
		}
	}
	// itens e subtotal
	if len(p.Itens) == 0 {
		return fmt.Errorf("%w: sem itens", ErrPedidoInvalido)
	}
	var subtotal int64
	var pecas int
	for _, it := range p.Itens {
		if it.Quantidade <= 0 || it.PrecoCent < 0 {
			return fmt.Errorf("%w: item %s inválido", ErrPedidoInvalido, it.SKU)
		}
		subtotal += int64(it.Quantidade) * it.PrecoCent
		pecas += it.Quantidade
	}
	// desconto por cupom
	var desconto int64
	switch strings.ToUpper(p.Cupom) {
	case "":
	case "BEMVINDO10":
		desconto = subtotal * 10 / 100
	case "FRETEGRATIS":
	case "ATACADO":
		if pecas >= 12 {
			desconto = subtotal * 15 / 100
		}
	default:
		return fmt.Errorf("%w: cupom %s desconhecido", ErrPedidoInvalido, p.Cupom)
	}
	// frete: grátis com cupom ou acima de R$ 300; senão por UF
	var frete int64
	if strings.ToUpper(p.Cupom) != "FRETEGRATIS" && subtotal-desconto < 30000 {
		switch strings.ToUpper(p.Entrega.UF) {
		case "SP":
			frete = 1590
		case "RJ", "MG", "PR":
			frete = 2290
		default:
			frete = 3490
		}
	}
	p.FreteCent = frete
	p.TotalCent = subtotal - desconto + frete
	return nil
}
