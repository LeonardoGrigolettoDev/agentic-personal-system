// Package assinaturas gerencia os planos do app de assinaturas da Nitro.
package assinaturas

import (
	"errors"
	"sync"
	"time"
)

type Assinatura struct {
	ID        string
	Cliente   string
	Plano     string
	Ativa     bool
	RenovaEm  time.Time
	ValorCent int64
}

var ErrNaoEncontrada = errors.New("assinatura não encontrada")

// Repositorio é onde as assinaturas ficam guardadas.
type Repositorio interface {
	Busca(id string) (Assinatura, error)
	Salva(a Assinatura) error
}

type Servico struct {
	repo   Repositorio
	precos map[string]int64
}

func NovoServico(repo Repositorio) *Servico {
	return &Servico{repo: repo, precos: map[string]int64{"basico": 1990, "pro": 4990}}
}

// Renova estende a assinatura por um mês a partir de `agora` e atualiza o preço do plano.
func (s *Servico) Renova(id string, agora time.Time) (Assinatura, error) {
	a, err := s.repo.Busca(id)
	if err != nil {
		return Assinatura{}, err
	}
	a.Ativa = true
	a.RenovaEm = agora.AddDate(0, 1, 0)
	a.ValorCent = s.precos[a.Plano]
	return a, s.repo.Salva(a)
}

// Cancela desativa a assinatura.
func (s *Servico) Cancela(id string) error {
	a, err := s.repo.Busca(id)
	if err != nil {
		return err
	}
	a.Ativa = false
	return s.repo.Salva(a)
}

// RepositorioMemoria é a implementação usada em produção enquanto não há banco.
type RepositorioMemoria struct {
	mu    sync.Mutex
	itens map[string]Assinatura
}

func NovoRepositorioMemoria() *RepositorioMemoria {
	return &RepositorioMemoria{itens: map[string]Assinatura{}}
}

func (r *RepositorioMemoria) Busca(id string) (Assinatura, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	a, ok := r.itens[id]
	if !ok {
		return Assinatura{}, ErrNaoEncontrada
	}
	return a, nil
}

func (r *RepositorioMemoria) Salva(a Assinatura) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.itens[a.ID] = a
	return nil
}
