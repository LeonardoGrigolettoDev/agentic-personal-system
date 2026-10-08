#!/usr/bin/env bash
# Monta ./repo com um histórico determinístico (datas e autores fixos).
set -euo pipefail
rm -rf repo && mkdir repo && cd repo
export GIT_AUTHOR_NAME="Leo" GIT_AUTHOR_EMAIL="leo@example.com" GIT_COMMITTER_NAME="Leo" GIT_COMMITTER_EMAIL="leo@example.com"
git -c init.defaultBranch=main init -q
n=0
commit() {
  n=$((n + 1))
  export GIT_AUTHOR_DATE="2026-09-$(printf '%02d' "$n")T10:00:00-03:00" GIT_COMMITTER_DATE="2026-09-$(printf '%02d' "$n")T10:00:00-03:00"
  git add -A && git -c commit.gpgsign=false commit -q -m "$1"
}

cat > frete.py <<'PY'
"""Frete da loja do mini-ERP."""


def frete(peso_kg, valor_pedido, uf):
    if valor_pedido >= 300:
        return 0.0
    base = 15.9 if uf == "SP" else 22.9
    if peso_kg > 5:
        base += (peso_kg - 5) * 2.5
    return round(base, 2)
PY
cat > test_frete.py <<'PY'
import unittest

from frete import frete


class TestFrete(unittest.TestCase):
    def test_gratis_a_partir_de_300(self):
        self.assertEqual(frete(1, 300, "SP"), 0.0)
        self.assertEqual(frete(1, 299.99, "SP"), 15.9)

    def test_peso(self):
        self.assertEqual(frete(7, 100, "RJ"), 27.9)


if __name__ == "__main__":
    unittest.main()
PY
commit "feat: cálculo de frete por UF e peso"
printf '# Loja\n\nFrete: `python3 -m unittest`\n' > README.md
commit "docs: README"
git tag v1.0
sed -i 's/22.9/23.9/' frete.py && sed -i 's/27.9/28.9/' test_frete.py
commit "fix: reajuste do frete fora de SP"
printf '\nUFs com tarifa reduzida: SP.\n' >> README.md
commit "docs: tarifas por UF"
cat >> frete.py <<'PY'


def prazo(uf):
    return 2 if uf == "SP" else 5
PY
commit "feat: prazo de entrega por UF"
printf 'venv/\n__pycache__/\n' > .gitignore
commit "chore: gitignore"
cat > frete.py <<'PY'
"""Frete da loja do mini-ERP."""

GRATIS_A_PARTIR_DE = 300
TARIFA = {"SP": 15.9}
TARIFA_PADRAO = 23.9


def frete(peso_kg, valor_pedido, uf):
    if valor_pedido > GRATIS_A_PARTIR_DE:
        return 0.0
    base = TARIFA.get(uf, TARIFA_PADRAO) + max(0, peso_kg - 5) * 2.5
    return round(base, 2)


def prazo(uf):
    return 2 if uf == "SP" else 5
PY
commit "refactor: simplifica cálculo do frete"
printf '\nPrazo: SP 2 dias, demais 5.\n' >> README.md
commit "docs: prazos"
sed -i 's/return 2 if uf == "SP" else 5/return {"SP": 2, "RJ": 3, "MG": 3}.get(uf, 5)/' frete.py
commit "feat: prazo menor para RJ e MG"
printf 'TARIFA_PADRAO=23.9\n' > tarifas.env
commit "chore: tarifas em arquivo de configuração"
sed -i 's/^Frete: /Testes do frete: /' README.md
commit "docs: como rodar os testes"
cat >> test_frete.py <<'PY'


class TestPrazo(unittest.TestCase):
    def test_prazo(self):
        from frete import prazo
        self.assertEqual(prazo("MG"), 3)
PY
commit "test: prazo por UF"
