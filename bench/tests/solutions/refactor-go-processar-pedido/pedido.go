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

func invalido(format string, args ...any) error {
	return fmt.Errorf("%w: "+format, append([]any{ErrPedidoInvalido}, args...)...)
}

// ProcessarPedido valida o pedido e calcula total, desconto e frete.
func ProcessarPedido(p *Pedido) error {
	if strings.TrimSpace(p.Cliente) == "" {
		return invalido("cliente vazio")
	}
	if err := validaEndereco("entrega", p.Entrega); err != nil {
		return err
	}
	if err := validaEndereco("cobrança", p.Cobranca); err != nil {
		return err
	}
	subtotal, pecas, err := somaItens(p.Itens)
	if err != nil {
		return err
	}
	cupom := strings.ToUpper(p.Cupom)
	desconto, err := descontoCupom(cupom, subtotal, pecas)
	if err != nil {
		return err
	}
	p.FreteCent = calculaFrete(cupom, p.Entrega.UF, subtotal-desconto)
	p.TotalCent = subtotal - desconto + p.FreteCent
	return nil
}

func validaEndereco(tipo string, e Endereco) error {
	if strings.TrimSpace(e.Rua) == "" || strings.TrimSpace(e.Numero) == "" {
		return invalido("%s: rua e número obrigatórios", tipo)
	}
	if len(e.UF) != 2 {
		return invalido("%s: UF inválida", tipo)
	}
	cep := strings.ReplaceAll(e.CEP, "-", "")
	if len(cep) != 8 || strings.Trim(cep, "0123456789") != "" {
		return invalido("%s: CEP inválido", tipo)
	}
	return nil
}

func somaItens(itens []Item) (subtotal int64, pecas int, err error) {
	if len(itens) == 0 {
		return 0, 0, invalido("sem itens")
	}
	for _, it := range itens {
		if it.Quantidade <= 0 || it.PrecoCent < 0 {
			return 0, 0, invalido("item %s inválido", it.SKU)
		}
		subtotal += int64(it.Quantidade) * it.PrecoCent
		pecas += it.Quantidade
	}
	return subtotal, pecas, nil
}

func descontoCupom(cupom string, subtotal int64, pecas int) (int64, error) {
	switch cupom {
	case "", "FRETEGRATIS":
		return 0, nil
	case "BEMVINDO10":
		return subtotal * 10 / 100, nil
	case "ATACADO":
		if pecas >= 12 {
			return subtotal * 15 / 100, nil
		}
		return 0, nil
	}
	return 0, invalido("cupom %s desconhecido", cupom)
}

func calculaFrete(cupom, uf string, valor int64) int64 {
	if cupom == "FRETEGRATIS" || valor >= 30000 {
		return 0
	}
	switch strings.ToUpper(uf) {
	case "SP":
		return 1590
	case "RJ", "MG", "PR":
		return 2290
	}
	return 3490
}
