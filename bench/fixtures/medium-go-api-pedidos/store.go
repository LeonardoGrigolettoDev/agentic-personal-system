package api

import (
	"errors"
	"sync"
)

type Pedido struct {
	ID      int     `json:"id"`
	Cliente string  `json:"cliente"`
	Total   float64 `json:"total"`
}

var ErrNaoEncontrado = errors.New("não encontrado")

// Store em memória (já pronto).
type Store struct {
	mu      sync.Mutex
	proximo int
	itens   map[int]Pedido
}

func NovoStore() *Store { return &Store{proximo: 1, itens: map[int]Pedido{}} }

func (s *Store) Cria(p Pedido) Pedido {
	s.mu.Lock()
	defer s.mu.Unlock()
	p.ID = s.proximo
	s.proximo++
	s.itens[p.ID] = p
	return p
}

func (s *Store) Busca(id int) (Pedido, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	p, ok := s.itens[id]
	if !ok {
		return Pedido{}, ErrNaoEncontrado
	}
	return p, nil
}
