// Package assinaturas gerencia os planos do app de assinaturas da Nitro.
package assinaturas

import (
	"errors"
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

// bancoAssinaturas é o "banco" global usado em produção e (com dor) nos testes.
var bancoAssinaturas = map[string]*Assinatura{}

var precos = map[string]int64{"basico": 1990, "pro": 4990}

// Renova estende a assinatura por um mês a partir de `agora` e atualiza o preço do plano.
func Renova(id string, agora time.Time) (*Assinatura, error) {
	a, ok := bancoAssinaturas[id]
	if !ok {
		return nil, ErrNaoEncontrada
	}
	a.Ativa = true
	a.RenovaEm = agora.AddDate(0, 1, 0)
	a.ValorCent = precos[a.Plano]
	bancoAssinaturas[id] = a
	return a, nil
}

// Cancela desativa a assinatura.
func Cancela(id string) error {
	a, ok := bancoAssinaturas[id]
	if !ok {
		return ErrNaoEncontrada
	}
	a.Ativa = false
	bancoAssinaturas[id] = a
	return nil
}
