package carrinho

import "testing"

func TestVariacoesIndependentes(t *testing.T) {
	base := make([]Item, 0, 8)
	base = append(base, Item{"CAFE-500G", 1}, Item{"FILTRO-103", 2})
	vars := Variacoes(base, []Item{{"ACUCAR-1KG", 1}, {"LEITE-1L", 6}})
	if len(vars) != 2 {
		t.Fatalf("len(vars) = %d", len(vars))
	}
	if got := vars[0][2].SKU; got != "ACUCAR-1KG" {
		t.Errorf("variação 0 terminou com %s, want ACUCAR-1KG", got)
	}
	if got := vars[1][2].SKU; got != "LEITE-1L" {
		t.Errorf("variação 1 terminou com %s, want LEITE-1L", got)
	}
	if len(base) != 2 {
		t.Errorf("base alterada: len = %d", len(base))
	}
}
