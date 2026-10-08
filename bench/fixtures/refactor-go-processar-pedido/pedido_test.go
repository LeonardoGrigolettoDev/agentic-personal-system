package vendas

import (
	"errors"
	"testing"
)

func base() *Pedido {
	end := Endereco{Rua: "Rua A", Numero: "10", Cidade: "Campinas", UF: "SP", CEP: "13010-000"}
	return &Pedido{Cliente: "Ana", Entrega: end, Cobranca: end,
		Itens: []Item{{"CAFE", 2, 1890}, {"FILTRO", 1, 750}}}
}

func TestTotais(t *testing.T) {
	casos := []struct {
		nome        string
		muda        func(*Pedido)
		total, frete int64
	}{
		{"sp sem cupom", func(p *Pedido) {}, 4530 + 1590, 1590},
		{"rj", func(p *Pedido) { p.Entrega.UF = "RJ" }, 4530 + 2290, 2290},
		{"ba", func(p *Pedido) { p.Entrega.UF = "BA" }, 4530 + 3490, 3490},
		{"bemvindo", func(p *Pedido) { p.Cupom = "bemvindo10" }, 4530 - 453 + 1590, 1590},
		{"frete gratis", func(p *Pedido) { p.Cupom = "FRETEGRATIS" }, 4530, 0},
		{"atacado sem volume", func(p *Pedido) { p.Cupom = "ATACADO" }, 4530 + 1590, 1590},
		{"atacado", func(p *Pedido) { p.Cupom = "ATACADO"; p.Itens[0].Quantidade = 12 },
			(12*1890 + 750) - (12*1890+750)*15/100 + 1590, 1590},
		{"acima de 300", func(p *Pedido) { p.Itens[0].Quantidade = 20 }, 20*1890 + 750, 0},
	}
	for _, c := range casos {
		p := base()
		c.muda(p)
		if err := ProcessarPedido(p); err != nil {
			t.Fatalf("%s: %v", c.nome, err)
		}
		if p.TotalCent != c.total || p.FreteCent != c.frete {
			t.Errorf("%s: total=%d frete=%d, want %d/%d", c.nome, p.TotalCent, p.FreteCent, c.total, c.frete)
		}
	}
}

func TestInvalidos(t *testing.T) {
	casos := map[string]func(*Pedido){
		"cliente":       func(p *Pedido) { p.Cliente = " " },
		"rua entrega":   func(p *Pedido) { p.Entrega.Rua = "" },
		"uf entrega":    func(p *Pedido) { p.Entrega.UF = "São Paulo" },
		"cep entrega":   func(p *Pedido) { p.Entrega.CEP = "1301-000" },
		"cep letras":    func(p *Pedido) { p.Entrega.CEP = "1301A-000" },
		"numero cobr":   func(p *Pedido) { p.Cobranca.Numero = "" },
		"uf cobranca":   func(p *Pedido) { p.Cobranca.UF = "X" },
		"cep cobranca":  func(p *Pedido) { p.Cobranca.CEP = "abc" },
		"sem itens":     func(p *Pedido) { p.Itens = nil },
		"qtd zero":      func(p *Pedido) { p.Itens[1].Quantidade = 0 },
		"cupom inexist": func(p *Pedido) { p.Cupom = "XPTO" },
	}
	for nome, muda := range casos {
		p := base()
		muda(p)
		if err := ProcessarPedido(p); !errors.Is(err, ErrPedidoInvalido) {
			t.Errorf("%s: err = %v, want ErrPedidoInvalido", nome, err)
		}
	}
}
