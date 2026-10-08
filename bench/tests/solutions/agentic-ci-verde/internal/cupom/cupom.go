// Package cupom valida cupons de desconto do app de vendas.
package cupom

import (
	"fmt"
	"strings"
	"time"
)

type Cupom struct {
	Codigo     string
	Percentual int
	ValidoAte  time.Time
	UsosMax    int
	Usos       int
}

// Valido diz se o cupom pode ser usado em `agora`. O cupom vale até o fim do dia de ValidoAte (inclusive).
func (c Cupom) Valido(agora time.Time) bool {
	fim := time.Date(c.ValidoAte.Year(), c.ValidoAte.Month(), c.ValidoAte.Day()+1, 0, 0, 0, 0, c.ValidoAte.Location())
	return agora.Before(fim) && c.Usos < c.UsosMax
}

// Descricao para o carrinho.
func (c Cupom) Descricao() string {
	return fmt.Sprintf("%s: %d%% de desconto (%d usos restantes)", strings.ToUpper(c.Codigo), c.Percentual, c.UsosMax-c.Usos)
}
