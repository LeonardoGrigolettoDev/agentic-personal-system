---
name: architecture_review
description: "Avaliar arquitetura ou mudança estrutural com trade-offs."
version: 1.0.0
author: Leonardo Grigoletto (agent-system)
license: Proprietary
platforms: [linux, macos]
metadata:
  hermes:
    category: coding
    tags: [coding, architecture, design, trade-offs, adr]
    related_skills: [repository_analysis, decision_record, code_review, web_research]
---

# Architecture Review

Avalia um desenho existente ou uma proposta de mudança estrutural (novo serviço, troca de banco,
divisão de módulos, contrato entre componentes) e termina numa recomendação com trade-offs explícitos
e, quando houver decisão, um ADR. Fatos do código vêm de ferramentas; opiniões vêm marcadas como tal.

## When to Use

- "Essa arquitetura faz sentido?", "como dividir X?", "devo trocar A por B?", RFC/design doc para revisar.
- `task_type=architecture` no roteamento, ou um `code_review` que encontrou mudança estrutural grande.

Não use para revisar um diff pequeno (`code_review`) nem para registrar uma decisão já tomada
(`decision_record`).

## Inputs

- A pergunta/proposta e as restrições conhecidas (custo, prazo, equipe, SLA, compliance).
- Repositório(s) no sandbox, quando houver código; tenant (`nitro` | `pessoal`) e projeto.

## Procedure

1. **Enquadrar.** Escreva o problema em 2–3 frases, os requisitos de qualidade que importam (ex.:
   latência, custo, operabilidade, isolamento de dados) e o horizonte (6 meses? 3 anos?).
2. **Contexto e decisões anteriores.**
   - `mcp__knowledge__compile_context(task="<pergunta>", tenant=<tenant>, domain="engineering", project=<slug>)`
   - `mcp__knowledge__memory_search(tenant=<tenant>, query="[projeto <slug>] <tema>", kind="decision")`
     e `kind="architecture"` (sem filtro `project`, que esconde memórias de projeto não cadastrado):
     não contradiga um ADR vigente sem dizer que está propondo substituí-lo.
3. **Estado atual por evidência** (código existente): rode `repository_analysis`; depois meça o que
   sustenta o argumento, por exemplo dependências entre pacotes (`go list -deps ./...`,
   `pnpm why <pkg>`), tamanho/acoplamento (`search_files` por imports), hot spots (`git log --format= --name-only | sort | uniq -c | sort -rn | head`).
4. **Opções.** No mínimo 2 alternativas reais + "não fazer nada". Para cada: como funciona, custo de
   mudança, custo de operação, riscos, reversibilidade, impacto em segurança/tenants e em testes.
5. **Pesquisa quando faltar fato externo** (limites de serviço, preço, maturidade de lib): skill
   `web_research` com fontes primárias; não estime de memória números que decidem a questão.
6. **Recomendação.** Uma opção, condições em que a escolha mudaria, e plano incremental (passos
   pequenos, cada um com validação). Marque incertezas.
7. **Registrar.** Se o usuário decidir, gere o ADR com a skill `decision_record` e salve a decisão:
   `mcp__knowledge__memory_save(tenant=<tenant>, scope="project", project=<slug>, domain="engineering",
   kind="decision", lifecycle="persistent", content="[projeto <slug>] <decisão + motivo + data>")`.
   Erro "unknown project" (projeto não cadastrado no knowledge) → repita com `scope="domain"` e o mesmo `content`, que começa com `[projeto <slug>]`.
8. **Delegar execução** quando aprovada: `kanban_create(title="...", assignee="engineering",
   tenant=<tenant>, body="<plano + critérios de aceite>", skills=["repository_analysis"])`.

## Outputs

```markdown
## Pergunta
## Requisitos e restrições
## Estado atual (com evidência)
## Opções
| Opção | Prós | Contras | Custo de mudança | Reversível? |
## Recomendação e plano incremental
## Riscos, incertezas e o que mudaria a decisão
```

## Gate e escalonamento

- Não há gate automático para revisão de arquitetura: o gate `aios` (`pre_verify`) só roda quando
  código é editado, e nada escala o modelo por palavras no texto. A validação é a evidência: cada
  afirmação com arquivo/comando/documento e trade-offs comparáveis entre as opções.
- Evidência fraca numa decisão crítica (dados, segurança, custo alto, irreversível): pare e diga o que
  falta medir; pergunte ao usuário (ou `kanban_block` com o motivo) em vez de recomendar no escuro.
- Nunca peça outro modelo. Ao ver "orçamento esgotado", não chame mais ferramentas: entregue a análise
  parcial com as lacunas marcadas.

## Pitfalls

- Recomendar tecnologia da moda sem requisito que a justifique; ignorar o custo de operação no 16 GB.
- Comparar opções em níveis de detalhe diferentes (uma idealizada, outra com todos os problemas).
- Misturar contextos de tenants: decisões do `nitro` não valem para projetos `pessoal`.

## Verification

- Cada afirmação sobre o sistema atual cita arquivo, comando ou documento; existe pelo menos uma
  alternativa descartada com motivo; o plano tem passos verificáveis.
