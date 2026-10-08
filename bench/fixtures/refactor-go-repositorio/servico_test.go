package assinaturas

import (
	"errors"
	"testing"
	"time"
)

// repoFalso implementa Repositorio em memória, só para o teste.
type repoFalso struct {
	itens  map[string]Assinatura
	salvos int
}

func (r *repoFalso) Busca(id string) (Assinatura, error) {
	a, ok := r.itens[id]
	if !ok {
		return Assinatura{}, ErrNaoEncontrada
	}
	return a, nil
}

func (r *repoFalso) Salva(a Assinatura) error {
	r.itens[a.ID] = a
	r.salvos++
	return nil
}

func TestRenovaComRepositorioInjetado(t *testing.T) {
	repo := &repoFalso{itens: map[string]Assinatura{"a1": {ID: "a1", Cliente: "Ana", Plano: "pro"}}}
	s := NovoServico(repo)
	agora := time.Date(2026, 10, 7, 12, 0, 0, 0, time.UTC)
	a, err := s.Renova("a1", agora)
	if err != nil {
		t.Fatal(err)
	}
	if !a.Ativa || a.ValorCent != 4990 || !a.RenovaEm.Equal(time.Date(2026, 11, 7, 12, 0, 0, 0, time.UTC)) {
		t.Fatalf("renovação errada: %+v", a)
	}
	if repo.salvos != 1 || !repo.itens["a1"].Ativa {
		t.Fatalf("não salvou no repositório: %+v", repo)
	}
}

func TestCancelaEErros(t *testing.T) {
	repo := &repoFalso{itens: map[string]Assinatura{"a2": {ID: "a2", Plano: "basico", Ativa: true}}}
	s := NovoServico(repo)
	if err := s.Cancela("a2"); err != nil || repo.itens["a2"].Ativa {
		t.Fatalf("cancelamento falhou: err=%v item=%+v", err, repo.itens["a2"])
	}
	if _, err := s.Renova("zz", time.Now()); !errors.Is(err, ErrNaoEncontrada) {
		t.Fatalf("err = %v, want ErrNaoEncontrada", err)
	}
	if err := s.Cancela("zz"); !errors.Is(err, ErrNaoEncontrada) {
		t.Fatalf("err = %v, want ErrNaoEncontrada", err)
	}
}

func TestRepositorioEmMemoriaDeProducao(t *testing.T) {
	var repo Repositorio = NovoRepositorioMemoria()
	if err := repo.Salva(Assinatura{ID: "x", Plano: "basico"}); err != nil {
		t.Fatal(err)
	}
	a, err := NovoServico(repo).Renova("x", time.Date(2026, 1, 31, 0, 0, 0, 0, time.UTC))
	if err != nil || a.ValorCent != 1990 {
		t.Fatalf("a=%+v err=%v", a, err)
	}
}
