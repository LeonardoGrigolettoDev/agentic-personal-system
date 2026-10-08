// Package metricas conta requisições por rota no gateway do app de entregas.
package metricas

import "sync"

// Contador é seguro para uso concorrente.
type Contador struct {
	mu      sync.Mutex
	total   int
	porRota map[string]int
}

func NovoContador() *Contador {
	return &Contador{porRota: map[string]int{}}
}

// Registra conta uma requisição para a rota.
func (c *Contador) Registra(rota string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.total++
	c.porRota[rota]++
}

func (c *Contador) Total() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.total
}

func (c *Contador) DaRota(rota string) int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.porRota[rota]
}
