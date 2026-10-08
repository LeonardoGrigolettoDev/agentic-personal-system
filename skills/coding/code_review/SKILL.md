---
name: code_review
description: "Revisar diff ou PR: bugs, riscos, testes, padrões do repo."
version: 1.0.0
author: Leonardo Grigoletto (agent-system)
license: Proprietary
platforms: [linux, macos]
metadata:
  hermes:
    category: coding
    tags: [coding, review, pull-request, quality]
    related_skills: [repository_analysis, test_generation, architecture_review, debugging]
---

# Code Review

Revisão focada em defeitos reais de um diff: corretude, segurança, regressões e cobertura de testes,
depois aderência às convenções do repositório. Ferramentas determinísticas (testes, lint, tipos)
rodam antes da leitura; o LLM revisa o que as ferramentas não pegam. Não reescreve o código do autor.

## When to Use

- "Revise este PR/diff/commit", loop implementer → reviewer (§7) ou `kanban_request_review`.
- Antes de concluir uma implementação própria (auto-revisão do diff final).

Não use para decidir arquitetura de um sistema inteiro: `architecture_review`.

## Inputs

- O diff: branch/commit/PR no sandbox (`git diff <base>...HEAD`) ou patch colado.
- Intenção da mudança (issue, descrição do PR, tarefa Kanban) e tenant (`nitro` | `pessoal`).

## Procedure

1. **Escopo.** Via `terminal` no repositório do sandbox:
   ```bash
   git fetch -q origin && git diff --stat origin/main...HEAD
   git diff origin/main...HEAD -- . ':(exclude)*.lock'
   ```
   Diff grande (> ~800 linhas)? Revise por módulo e diga o que ficou de fora.
2. **Checagens determinísticas primeiro.**
   ```bash
   aios-check --json /workspace/tasks/<id>       # testes do repo
   ```
   Rode também o lint/typecheck detectado por `repository_analysis` (ex.: `go vet ./...`,
   `uv run ruff check .`, `pnpm exec tsc --noEmit`, `pnpm exec eslint .`). Falha aqui já é achado.
3. **Convenções e contexto.** `mcp__knowledge__compile_context(task="review: <intenção>", tenant=<tenant>,
   domain="engineering", project=<slug>)` traz regras/decisões do projeto (ex.: padrões de erro, ADRs).
4. **Leitura do diff** com o checklist de `references/checklist.md`, nesta prioridade:
   corretude → segurança → concorrência/recursos → contratos/compatibilidade → testes → clareza.
   Para cada suspeita, abra o código ao redor com `read_file` e confirme; se der, prove com um teste
   ou comando. Achado sem cenário concreto de falha vira pergunta, não defeito.
5. **Classifique** cada achado: `bloqueante` (bug, vulnerabilidade, perda de dados, contrato quebrado),
   `importante` (risco provável, teste faltando em caminho crítico), `sugestão` (clareza, estilo).
   Sem "nitpicks" que o lint deveria pegar.
6. **Veredito**: `aprovar`, `aprovar com ressalvas` ou `pedir mudanças`. Em Kanban, use
   `kanban_request_changes(reason=...)` ou `kanban_complete(summary=...)`.
7. Padrões recorrentes do time (ex.: "sempre validar tenant") vão para memória:
   `mcp__knowledge__memory_save(tenant=<tenant>, scope="project", kind="rule", project=<slug>,
   domain="engineering", content="[projeto <slug>] <regra>")`. Erro "unknown project" (projeto não cadastrado no knowledge) → repita com `scope="domain"` e o mesmo `content`, que começa com `[projeto <slug>]`.

## Outputs

```markdown
**Veredito:** pedir mudanças — 1 bloqueante, 2 importantes
**Checks:** testes ok (`go test ./...`) · lint 1 aviso
1. [bloqueante] `api/orders.go:88` — erro de `tx.Commit()` ignorado: pedido some se o commit falhar.
   Cenário: ... · Sugestão: retornar o erro e cobrir com teste de falha de commit.
2. [importante] ...
**Fora do escopo revisado:** <arquivos/razão>
```

## Gate e escalonamento

- Revisão não edita código, então o gate `aios` (`pre_verify`) não roda aqui e nada escala o modelo
  por palavras no veredito. A validação é a evidência: checks executados e cenário concreto por achado.
  Se pedirem correção, a implementação passa pelo gate normalmente.
- Mudança arquitetural grande ou risco crítico (segurança, perda de dados) sem certeza: veredito
  `pedir mudanças` com a dúvida explícita e peça revisão humana (`kanban_request_changes`/`kanban_block`
  com o motivo, ou pergunte ao usuário); para desenho, sugira `architecture_review`.
- Nunca peça outro modelo. Ao ver "orçamento esgotado", não chame mais ferramentas: entregue os
  achados já confirmados e o que ficou sem revisar.

## Pitfalls

- Revisar sem rodar os testes; aprovar diff que não compila.
- Comentar estilo e perder o bug; inventar problema sem caminho de execução real.
- Diff com segredos (tokens, `.env`): bloqueante imediato; não reproduza o valor no comentário.

## Verification

- Todo achado `bloqueante` cita arquivo:linha e um cenário concreto; os checks determinísticos foram
  executados e seus resultados aparecem no relatório.
