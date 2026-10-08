---
name: spreadsheet_analysis
description: "Analisar planilha CSV/XLSX: totais, categorias e meses."
version: 1.0.0
author: Leonardo Grigoletto (agent-system)
license: Proprietary
platforms: [linux, macos]
metadata:
  hermes:
    category: finance
    tags: [finance, spreadsheet, csv, xlsx, duckdb, deterministic]
    related_skills: [budget_review, expense_categorization]
    requires_toolsets: [terminal]
---

# Spreadsheet Analysis

Perfil determinístico de uma planilha (extrato, fatura, controle de gastos, relatório da empresa):
colunas e tipos, contagem de linhas, totais, somas por categoria e por mês, variação mês a mês,
categoria × mês e maiores lançamentos. Os números vêm do script; o LLM interpreta e recomenda.
Nunca executa transações nem altera a planilha original.

## When to Use

- "Analise esta planilha/extrato", "quanto gastei por categoria/mês", "o que mudou de agosto para setembro".
- Primeiro passo de `budget_review` e de `expense_categorization` (normaliza a planilha).

## Inputs

- Arquivo `.csv`, `.xlsx`/`.xlsm` no sandbox (ex.: `/workspace/financas/2026-09.csv`); aba, se não for a primeira.
- Tenant: `pessoal` (finanças pessoais) ou `nitro` (empresa/trabalho). Domínio de conhecimento: `finance`.

## How to Run

Via `terminal` no sandbox:

```bash
D="${HERMES_SKILL_DIR}"; [ -d "$D" ] || D=$(ls -d ~/.hermes/external_skills/*/finance/spreadsheet_analysis 2>/dev/null | head -n 1)
python3 "$D/scripts/analyze.py" /workspace/financas/extrato.csv --top 10 > /workspace/financas/extrato.analysis.json
```

| Opção | Uso |
|---|---|
| `--sheet NOME` | aba do XLSX |
| `--date-col/--amount-col/--category-col/--description-col` | força a coluna quando a detecção errar |
| `--debit-col X --credit-col Y` | extratos com débito e crédito separados (valor = crédito − débito) |
| `--decimal comma\|dot`, `--date-order dmy\|mdy`, `--delimiter ';'` | formatos fora do padrão BR |
| `--skip-rows N` | linhas antes do cabeçalho (preâmbulo de extrato); padrão: detectado |
| `--engine python\|duckdb` | força o leitor (padrão: duckdb se instalado, senão Python puro) |
| `--export-csv out.csv` | grava a tabela normalizada (UTF-8, vírgula) para outros scripts |

Saída JSON: `row_count`, `columns[]` (tipo, nulos, distintos, min/max/soma), `detected` (papéis das
colunas), `totals` (soma, entradas, saídas), `by_category`, `by_month`, `mom`, `by_category_month`,
`top`, `warnings`. Despesas negativas e receitas positivas, como no extrato.

## Procedure

1. **Rodar o script** (acima). Leia `warnings` e `detected` primeiro: se a coluna de valor/data foi
   detectada errado, rode de novo com `--amount-col`/`--date-col`. Não "corrija" números à mão.
2. **Validar a leitura**: `row_count` bate com o esperado? `columns[].invalid` > 0 indica formatos
   mistos; investigue as linhas citadas nos avisos antes de concluir.
3. **Contexto do usuário**: `mcp__knowledge__compile_context(task="análise financeira <período>",
   tenant=<tenant>, domain="finance")` e `mcp__knowledge__memory_search(tenant=<tenant>,
   query="orçamento metas categorias", domain="finance")` para metas, orçamento e regras conhecidas.
4. **Perguntas adicionais** que o JSON não responde: consultas pontuais com duckdb sobre o CSV
   exportado, sempre mostrando a consulta:
   ```bash
   duckdb -c "SELECT categoria, count(*) FROM read_csv('/workspace/financas/out.csv', all_varchar=true) GROUP BY 1"
   ```
5. **Interpretar**: maiores categorias, variações relevantes (`mom` e `by_category_month`), gastos
   atípicos (`top`), recorrências. Toda afirmação numérica deve vir do JSON ou de uma consulta exibida.
6. **Recomendar** ações concretas e reversíveis (cortar assinatura X, revisar categoria Y). Nada de
   transferências, pagamentos ou contato com bancos.
7. **Salvar** só conclusões duráveis: `mcp__knowledge__memory_save(tenant=<tenant>, scope="domain",
   domain="finance", kind="fact", lifecycle="important", content="Set/2026: gasto total R$ X; maior
   categoria Y (Z%)")`. Não salve extratos nem dados linha a linha.

## Outputs

```markdown
## Planilha <arquivo> — <período> (<n> linhas)
- Entradas R$ X · Saídas R$ Y · Saldo R$ Z
- Top categorias: 1) Moradia R$ … (…%) 2) …
- Mês a mês: set vs ago: −R$ … (−…%) — principais causas: …
- Atípicos: <data> <descrição> R$ …
- Recomendações: …
- Avisos da leitura: <warnings relevantes>
```

## Gate e escalonamento

- Não há validador automático para análise financeira: o gate `aios` (`pre_verify`) só roda quando
  código é editado e nada escala o modelo por você relatar inconsistência. A validação é sua: totais
  citados = JSON do script, `warnings` tratados.
- Números que não fecham (soma ≠ total do extrato, `row_count` diferente do esperado, colunas mal
  detectadas mesmo com `--*-col`): pare, mostre a divergência e pergunte ao usuário (ou `kanban_block`
  com o motivo) em vez de concluir sobre dados errados.
- Nunca peça outro modelo. Ao ver "orçamento esgotado", não chame mais ferramentas: entregue os
  números já calculados.

## Pitfalls

- CSV de banco em Windows-1252 e com `;`: o script detecta; confira acentos nas categorias.
- "1.234" pode ser mil e duzentos (BR) ou 1,234 (US): veja `columns[].decimal`; force `--decimal`.
- Extrato com preâmbulo ("Extrato…", "Agência…", linha em branco): o aviso "preâmbulo … ignorada(s)"
  diz onde o cabeçalho foi achado; se errou, rode com `--skip-rows N`.
- Coluna "Tipo" (PIX, TED, Débito) não é categoria; só `categoria`/`category` (ou classe, grupo, centro
  de custo, "tipo de despesa") são detectadas. Confira `detected.category`; force com `--category-col`.
- Datas XLSX vêm como número serial; só são convertidas em colunas com nome de data ou via `--date-col`.
- Faturas de cartão listam despesas como positivas: o sinal muda a leitura de entradas/saídas.

## Verification

- `python3 -m json.tool` aceita a saída, `warnings` foi tratado e os totais citados conferem com
  `totals`/`by_category` do JSON.
