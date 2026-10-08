// Package entrega calcula a sequência de paradas dos motoboys do app de entregas.
package entrega

import "errors"

// Parada é um ponto de entrega.
type Parada struct {
	PedidoID int
	Lat, Lng float64
	Entregue bool
}

// Rota é a lista ordenada de paradas de um motoboy.
type Rota struct {
	MotoboyID int
	Paradas   []Parada
}

var ErrRotaConcluida = errors.New("rota concluída")

// ProximaParada devolve a primeira parada ainda não entregue depois da posição atual.
func (r *Rota) ProximaParada(atual int) (Parada, error) {
	if atual < 0 {
		atual = 0
	}
	// percorre as paradas restantes procurando a próxima pendente
	for i := atual + 1; i <= len(r.Paradas); i++ {
		if !r.Paradas[i].Entregue {
			return r.Paradas[i], nil
		}
	}
	return Parada{}, ErrRotaConcluida
}

// Pendentes conta quantas paradas ainda não foram entregues.
func (r *Rota) Pendentes() int {
	n := 0
	for _, p := range r.Paradas {
		if !p.Entregue {
			n++
		}
	}
	return n
}
