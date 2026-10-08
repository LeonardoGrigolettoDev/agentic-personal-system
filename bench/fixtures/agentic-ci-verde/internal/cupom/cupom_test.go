package cupom

import (
	"reflect"
	"testing"
	"time"
)

var sp = time.FixedZone("BRT", -3*3600)

func TestValidoAteFimDoDia(t *testing.T) {
	c := Cupom{Codigo: "black10", Percentual: 10, ValidoAte: time.Date(2026, 11, 27, 0, 0, 0, 0, sp), UsosMax: 3}
	if !c.Valido(time.Date(2026, 11, 27, 23, 59, 0, 0, sp)) {
		t.Error("deveria valer no último dia")
	}
	if c.Valido(time.Date(2026, 11, 28, 0, 0, 0, 0, sp)) {
		t.Error("não deveria valer no dia seguinte")
	}
	c.Usos = 3
	if c.Valido(time.Date(2026, 11, 20, 12, 0, 0, 0, sp)) {
		t.Error("usos esgotados")
	}
}

func TestDescricao(t *testing.T) {
	c := Cupom{Codigo: "bemvindo", Percentual: 15, UsosMax: 5, Usos: 2}
	if got := c.Descricao(); got != "BEMVINDO: 15% de desconto (3 usos restantes)" {
		t.Errorf("Descricao() = %q", got)
	}
}

func TestNormaliza(t *testing.T) {
	got := Normaliza([]string{" black10", "BLACK10", "", "frete "})
	if !reflect.DeepEqual(got, []string{"BLACK10", "FRETE"}) {
		t.Errorf("Normaliza = %v", got)
	}
}
