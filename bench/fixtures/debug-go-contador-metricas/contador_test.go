package metricas

import (
	"sync"
	"testing"
)

func TestContadorConcorrente(t *testing.T) {
	c := NovoContador()
	var wg sync.WaitGroup
	for i := 0; i < 200; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			if i%2 == 0 {
				c.Registra("/v1/pedidos")
			} else {
				c.Registra("/v1/entregas")
			}
		}(i)
	}
	wg.Wait()
	if got := c.Total(); got != 200 {
		t.Fatalf("Total() = %d, want 200", got)
	}
	if got := c.DaRota("/v1/pedidos"); got != 100 {
		t.Fatalf("DaRota(/v1/pedidos) = %d, want 100", got)
	}
}
