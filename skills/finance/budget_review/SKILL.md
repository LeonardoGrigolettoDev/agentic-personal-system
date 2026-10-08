---
name: budget_review
description: "Comparar gastos do mês com o orçamento por categoria."
version: 1.0.0
author: Leonardo Grigoletto (agent-system)
license: Proprietary
platforms: [linux, macos]
metadata:
  hermes:
    category: finance
    tags: [finance, budget, planning, deterministic]
    related_skills: [spreadsheet_analysis, expense_categorization, weekly_review]
    requires_toolsets: [terminal]
---

# Budget Review

Orçado × realizado por categoria para um mês: quanto foi gasto, quanto sobra, o que estourou, o que
está perto do limite (padrão 80%) e o que foi gasto sem orçamento. O cálculo é do script
`scripts/budget_check.py` sobre o JSON da skill `spreadsheet_analysis`; o LLM explica causas e propõe
ajustes. Não move dinheiro, não paga contas, não altera o orçamento sem aprovação.

## When to Use

- "Como está meu orçamento este mês?", fechamento mensal, revisão semanal de finanças.
- Depois de categorizar um extrato (`expense_categorization`).

## Inputs

- Planilha de lançamentos com data, valor e categoria (ou CSV já categorizado por `categorize.py`).
- Orçamento mensal: arquivo YAML/JSON `Categoria: limite` no workspace (ex.:
  `/workspace/financas/orcamento.yaml`). Se não existir, procure em memória
  (`mcp__knowledge__memory_search(tenant=<tenant>, query="orçamento mensal limites", domain="finance",
  kind="rule")`) e proponha um arquivo para o usuário aprovar.
- Mês (`YYYY-MM`; padrão: o último mês com lançamentos) e tenant (`pessoal` | `nitro`).

## How to Run

```bash
A="${HERMES_SKILL_DIR}"; [ -d "$A" ] || A=$(ls -d ~/.hermes/external_skills/*/finance/budget_review 2>/dev/null | head -n 1)
S=$(dirname "$A")/spreadsheet_analysis
python3 "$S/scripts/analyze.py" /workspace/financas/2026-09.csv --category-col categoria \
  | python3 "$A/scripts/budget_check.py" - --budget /workspace/financas/orcamento.yaml --month 2026-09
```

Formato do orçamento (YAML plano ou JSON; valores positivos, mesma grafia das categorias). Valores em
texto seguem o padrão BR: `1.500` = mil e quinhentos, `1.500,50`/`1500,50` com centavos; `1,500` é
ambíguo e o script recusa (escreva `1500`).

```yaml
Alimentação: 1500
Moradia: 2.000,00
"Lazer & Cultura": 400
```

Saída: `categories[]` com `budget`, `spent`, `remaining`, `used_pct`, `status` (`over` | `warn` | `ok`),
`unbudgeted[]`, `unused_budget[]`, `totals`, `counts`, `expense_sign` (detectado; force com `--sign`).

## Procedure

1. Garanta categorias: se a planilha não tem coluna de categoria, rode antes `expense_categorization`.
2. Rode o pipeline acima. Erro "análise sem by_category_month" = faltam colunas de data/categoria.
3. Confira `expense_sign`: extrato bancário → `negative`; lista de despesas/fatura → `positive`.
4. Para cada `over`/`warn`, explique a causa com os maiores lançamentos da categoria (campo `top` do
   `analyze.py` ou consulta duckdb exibida) e compare com meses anteriores (`by_category_month`).
5. `unbudgeted` relevante: proponha criar a categoria no orçamento ou recategorizar.
6. Recomende no máximo 3 ações priorizadas por impacto em R$; ajustes de limite são propostas para o
   usuário aprovar, nunca alterações silenciosas no arquivo.
7. Persistência: limites aprovados → `mcp__knowledge__memory_save(tenant=<tenant>, scope="domain",
   domain="finance", kind="rule", lifecycle="persistent", content="Orçamento mensal: Alimentação 1500; ...")`.
   Resultado do mês → `kind="fact"`, `lifecycle="important"` (uma linha com totais e estouros).

## Outputs

```markdown
## Orçamento <mês> — gasto R$ X de R$ Y (Z%)
- Estourado: Alimentação R$ 1.720 / 1.500 (115%) — causa: 6 pedidos de delivery (R$ 410)
- Atenção (≥80%): Transporte 86%
- Sem orçamento: Saúde R$ 380
- Ações sugeridas: 1) … 2) …
```

## Gate e escalonamento

- Não há validador automático para orçamento (o gate `aios` só roda quando código é editado): a
  evidência é o JSON do `budget_check.py` — cada estouro citado com `spent`/`budget`/`used_pct` dele.
- Orçamento ausente ou números incoerentes (gasto total muito diferente do extrato, `expense_sign`
  errado): pare e pergunte ao usuário antes de recomendar cortes.
- Nunca peça outro modelo. Ao ver "orçamento esgotado" (da tarefa), não chame mais ferramentas:
  entregue o resultado já calculado.

## Pitfalls

- Categorias com grafia diferente ("Alimentacao" × "Alimentação") contam como sem orçamento: padronize.
- Transferências entre contas próprias e investimentos não são gasto: categorize-as à parte e deixe
  fora do orçamento.
- Mês parcial (ainda em curso): diga a fração do mês decorrida antes de alarmar com `warn`.

## Verification

- `counts` e `totals` do JSON batem com o resumo; toda categoria citada aparece em `categories[]` ou
  `unbudgeted[]`.
