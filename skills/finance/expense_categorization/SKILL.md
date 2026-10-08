---
name: expense_categorization
description: "Categorizar transações por regras; LLM só nas sobras."
version: 1.0.0
author: Leonardo Grigoletto (agent-system)
license: Proprietary
platforms: [linux, macos]
metadata:
  hermes:
    category: finance
    tags: [finance, categorization, rules, deterministic]
    related_skills: [spreadsheet_analysis, budget_review]
    requires_toolsets: [terminal]
---

# Expense Categorization

Categoriza lançamentos com regras palavra-chave → categoria (zero tokens) e entrega ao LLM só os
desconhecidos, agrupados por estabelecimento. O LLM propõe regras novas; o usuário aprova; o script
roda de novo. Assim a cobertura cresce a cada mês e o custo cai.

## When to Use

- Extrato/fatura sem categoria ou com categorias inconsistentes, antes de `budget_review`.
- "Classifique meus gastos", "por que tanto em Outros?".

## Inputs

- Transações `.csv` (qualquer separador; UTF-8 ou Windows-1252) ou `.json`/`.jsonl`. Para XLSX ou
  formatos estranhos, normalize antes com `spreadsheet_analysis` (`analyze.py ... --export-csv out.csv`).
- Arquivo de regras do usuário (ex.: `/workspace/financas/regras.yaml`). Se não existir, comece de
  `templates/rules.example.yaml` e peça aprovação para salvar.
- Tenant: `pessoal` | `nitro`; domínio de conhecimento `finance`.

## How to Run

```bash
D="${HERMES_SKILL_DIR}"; [ -d "$D" ] || D=$(ls -d ~/.hermes/external_skills/*/finance/expense_categorization 2>/dev/null | head -n 1)
[ -f /workspace/financas/regras.yaml ] || cp "$D/templates/rules.example.yaml" /workspace/financas/regras.yaml
python3 "$D/scripts/categorize.py" /workspace/financas/extrato.csv --rules /workspace/financas/regras.yaml \
  --output-csv /workspace/financas/extrato.categorizado.csv
```

Opções: `--desc-col COL` (repetível: casa em várias colunas), `--amount-col COL`, `--decimal comma|dot`
(padrão: detectado uma vez para a coluna inteira, como no `analyze.py`), `--rows` (resultado linha a
linha no JSON), `--top-unknown N`. Saída: `coverage_pct`, `by_category[]`, `unknown_groups[]`
(`key`, `count`, `total`, `examples`, `rows`). O CSV de saída ganha as colunas `categoria` e `regra`.

Formato das regras (YAML ou JSON): `categories: {Categoria: [palavra, "duas palavras", "re:regex"]}`.
Palavras casam inteiras, sem diferenciar maiúsculas/acentos; `re:` usa regex sobre o texto normalizado
(minúsculo, sem acento). A correspondência mais longa vence ("uber eats" ganha de "uber").

## Procedure

1. Regras conhecidas: além do arquivo, consulte `mcp__knowledge__memory_search(tenant=<tenant>,
   query="regras de categorização", domain="finance", kind="rule")`.
2. Rode o script. Se `coverage_pct` ≥ 95%, pule para o passo 5.
3. Para cada grupo em `unknown_groups` (do maior para o menor), proponha uma regra em tabela:
   `key → categoria sugerida → palavra-chave`. Use as categorias existentes; crie categoria nova só com
   justificativa. Transferências entre contas próprias, aplicações e resgates vão para uma categoria
   própria, fora de gastos. Na dúvida, marque "perguntar".
4. Mostre a tabela, aplique só as regras aprovadas no arquivo (com `patch`) e rode de novo. Repita até
   a cobertura aceitável ou o usuário encerrar.
5. Entregue o CSV categorizado e o resumo por categoria; siga para `budget_review` se pedido.
6. Memória: regras aprovadas e estáveis → `mcp__knowledge__memory_save(tenant=<tenant>, scope="domain",
   domain="finance", kind="rule", lifecycle="persistent", content="Categorização: 'drogasil' → Saúde; ...")`.

## Outputs

```markdown
Cobertura: 92,4% (231/250) · Desconhecidos: 19 em 7 grupos
| Grupo | Qtde | Total | Sugestão | Regra proposta |
| pix enviado joao silva | 4 | −R$ 600 | Transferências | "re:^pix enviado .*joao silva" (perguntar) |
Arquivo: /workspace/financas/extrato.categorizado.csv
```

## Gate e escalonamento

- Não há validador automático para categorização (o gate `aios` só roda quando código é editado): a
  evidência é o JSON do script — `coverage_pct`, `by_category` e os grupos desconhecidos com exemplos.
- Grupo que você não sabe categorizar com segurança fica "perguntar"; não chute para subir a cobertura.
- Nunca peça outro modelo. Ao ver "orçamento esgotado", não chame mais ferramentas: entregue a tabela
  de regras propostas até ali.

## Pitfalls

- Extrato com linhas antes do cabeçalho ("Extrato…", "Agência…"): o script lê a 1ª linha como
  cabeçalho e não acha a descrição. Normalize antes com `analyze.py <arquivo> --export-csv out.csv`.
- Palavras curtas e genéricas ("pix", "pag", "compra") categorizam errado: prefira nome do estabelecimento.
- Estornos aparecem positivos na categoria do gasto: são esperados; não crie regra de receita para eles.
- Nunca grave regras sem aprovação; nunca edite o extrato original (o script escreve em outro arquivo).

## Verification

- Após aplicar as regras aprovadas, `coverage_pct` subiu e nenhum grupo antes categorizado mudou de
  categoria sem intenção (compare `by_category` entre as execuções).
