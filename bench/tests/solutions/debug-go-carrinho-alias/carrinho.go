// Package carrinho monta variações de carrinho para o simulador de frete do mini-ERP.
package carrinho

// Item de um carrinho.
type Item struct {
	SKU        string
	Quantidade int
}

// ComItem devolve um NOVO carrinho com o item adicionado. O carrinho recebido nunca é alterado: o simulador
// gera várias variações a partir do mesmo carrinho base.
func ComItem(base []Item, novo Item) []Item {
	out := make([]Item, len(base), len(base)+1)
	copy(out, base)
	return append(out, novo)
}

// Variacoes gera uma variação do carrinho base para cada item candidato.
func Variacoes(base []Item, candidatos []Item) [][]Item {
	out := make([][]Item, 0, len(candidatos))
	for _, c := range candidatos {
		out = append(out, ComItem(base, c))
	}
	return out
}
