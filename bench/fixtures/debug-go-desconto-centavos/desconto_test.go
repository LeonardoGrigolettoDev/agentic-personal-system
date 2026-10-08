package pedidos

import "testing"

func TestAplicaDesconto(t *testing.T) {
	casos := []struct {
		total int64
		pct   float64
		want  int64
	}{
		{10000, 10, 9000},
		{1999, 15, 1699}, // desconto 299,85 -> 300
		{333, 50, 166},   // desconto 166,5 -> 167
		{0, 30, 0},
		{12345, 0, 12345},
		{999, 100, 0},
	}
	for _, c := range casos {
		if got := AplicaDesconto(c.total, c.pct); got != c.want {
			t.Errorf("AplicaDesconto(%d, %v) = %d, want %d", c.total, c.pct, got, c.want)
		}
	}
}

func TestTotalComFrete(t *testing.T) {
	if got := TotalComFrete(1999, 15, 990); got != 2689 {
		t.Errorf("TotalComFrete = %d, want 2689", got)
	}
}
