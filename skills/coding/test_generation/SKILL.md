---
name: test_generation
description: "Escrever testes que falham antes e passam após a mudança."
version: 1.0.0
author: Leonardo Grigoletto (agent-system)
license: Proprietary
platforms: [linux, macos]
metadata:
  hermes:
    category: coding
    tags: [coding, testing, tdd, regression, sandbox]
    related_skills: [repository_analysis, debugging, code_review]
    requires_toolsets: [terminal]
---

# Test Generation

Gera testes úteis e determinísticos no estilo do repositório: primeiro um teste que falha pelo motivo
certo, depois a implementação/correção, depois a suíte verde. Testes são a evidência que o gate do
plugin `aios` usa para decidir `done`, `repair` ou `escalate`.

## When to Use

- Implementar feature ou corrigir bug (`task_type` implementation/debugging) — antes do código.
- "Aumente a cobertura de X", "escreva testes para este módulo", teste de regressão após incidente.

Não use para testes de carga/benchmarks nem para E2E que exigem serviços externos sem fake.

## Inputs

- Comportamento esperado (critérios de aceite, issue, bug reproduzido) e o módulo alvo.
- Repositório no sandbox (`/workspace/tasks/<id>`) com comando de teste conhecido (via `repository_analysis`).

## Procedure

1. **Descobrir o padrão local.** Com `search_files`, encontre 2–3 testes vizinhos do módulo alvo e
   imite: framework (go test/testify, pytest, vitest/jest), fixtures, nomes, tabela de casos, fakes.
   Não introduza framework novo sem pedido explícito.
2. **Listar casos** antes de escrever: caminho feliz, bordas (vazio, zero, limites, Unicode, datas),
   erros esperados, e o caso do bug/feature. Priorize por risco; 3–8 casos bons > 30 triviais.
3. **Escrever o teste que falha** com `write_file`/`patch` e rodá-lo isolado:
   ```bash
   cd /workspace/tasks/<id>
   go test ./internal/x -run 'TestParse' -count=1
   uv run pytest -q tests/test_parse.py -x
   pnpm exec vitest run src/parse.test.ts
   ```
   Confirme que falha pela razão esperada (asserção), não por erro de import/compilação.
4. **Implementar/corrigir** o código mínimo e rodar de novo o teste isolado até passar.
5. **Suíte completa e checagem do sandbox:**
   ```bash
   aios-check --json /workspace/tasks/<id>       # {exit_code, output_tail, command}
   ```
6. **Determinismo.** Nada de rede real, relógio real, aleatoriedade sem seed, ordem de map, sleeps.
   Injete relógio/IDs; use `tmp_path`/`t.TempDir()`; fakes em memória para HTTP/banco.
7. **Relatar** casos cobertos e lacunas conscientes. Convenções de teste descobertas que não são
   óbvias vão para memória (`mcp__knowledge__memory_save(tenant=<tenant>, scope="project",
   kind="procedure", project=<slug>, domain="engineering", content="[projeto <slug>] ...")`).
   Erro "unknown project" (projeto não cadastrado no knowledge) → repita com `scope="domain"` e o mesmo `content`, que começa com `[projeto <slug>]`.

## Gate e escalonamento

- Após editar código, o hook `pre_verify` do `aios` roda `aios-check` e consulta o gate.
- `[aios gate: repair]`: leia o `output_tail`, corrija a causa e não "afrouxe" o teste.
- `[aios gate: escalate]`: o modelo já foi trocado pelo Decision Service; continue com o mesmo plano.
- `[aios gate: fail|ask_human]` ou orçamento esgotado: pare e reporte o estado (testes escritos,
  o que falha e por quê). Nunca peça para trocar de modelo.

## Outputs

```markdown
**Testes:** `tests/test_parse.py` (+5 casos: vazio, decimal com vírgula, negativo entre parênteses, ...)
**Antes:** 2 falhando (bug reproduzido) · **Depois:** suíte 148 ok (`uv run pytest -q`)
**Lacunas:** <o que não foi coberto e por quê>
```

## Pitfalls

- Teste que passa sem a mudança (não prova nada) — sempre veja-o falhar primeiro.
- Snapshot gigante como única asserção; mocks que reimplementam a lógica testada.
- Alterar asserções existentes para "fazer passar" sem justificar no relatório.
- `aios-check` com `skipped: true` não validou nada (comando de teste não detectado): rode o comando
  do repositório explicitamente e mostre a saída como evidência.

## Verification

- O teste novo falha no código antigo e passa no novo; `aios-check` retorna `exit_code: 0`.
