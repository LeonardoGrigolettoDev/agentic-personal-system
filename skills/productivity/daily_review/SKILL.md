---
name: daily_review
description: "Revisão do dia: feito, pendente, aprendizados, ajustes."
version: 1.0.0
author: Leonardo Grigoletto (agent-system)
license: Proprietary
platforms: [linux, macos]
metadata:
  hermes:
    category: productivity
    tags: [productivity, review, reflection, daily, kanban]
    related_skills: [daily_planning, weekly_review, knowledge_capture]
---

# Daily Review

Fecha (ou abre) o dia com base no que de fato aconteceu: compara o plano com tarefas concluídas,
bloqueadas e paradas; registra pendências para amanhã; extrai aprendizados que valem memória; aponta
um ajuste concreto. Dois modos: **manhã** (revisão de ontem + panorama de hoje) e **noite**
(reflexão do dia). Curto: lê em 1 minuto.

## When to Use

- Execuções agendadas "morning-review" (07:30, modo manhã) e "daily-reflection" (22:30, modo noite).
- "Como foi meu dia?", "o que ficou pendente?", "revise ontem".

## Inputs

- Data e modo (manhã/noite), tenant da execução (`pessoal` ou `nitro`).
- Opcional: comentários do usuário sobre o dia (energia, imprevistos).

## Procedure

1. **Plano e registros do período** (tenant da sessão em todas as chamadas):
   - `mcp__knowledge__memory_search(tenant=<tenant>, query="<data> plano do dia revisão",
     domain="chief", kind="episode", k=15)` — o plano gravado por `daily_planning` e revisões
     anteriores; use só as que começam com a data do período (a busca ordena por similaridade).
   - `kanban_list(tenant=<tenant>)`: concluídas nas últimas 24 h, bloqueadas (motivo), em andamento
     sem atualização há > 2 dias.
   - Notas novas do dia (diário, reuniões, aulas): `mcp__knowledge__knowledge_search(tenant=<tenant>,
     query="<data> diário reunião aula", k=8)`.
2. **Comparar plano × realizado**: cada prioridade → `feito` | `parcial` | `não feito` (+ motivo
   provável). Sem plano gravado, use as tarefas concluídas como base e diga isso.
3. **Pendências**: o que fica para amanhã, com o próximo passo concreto. Bloqueios que dependem do
   usuário vão destacados.
4. **Aprendizados**: 0–3 itens que mudariam decisões futuras (padrão de distração, estimativa errada,
   o que funcionou). Só os duráveis viram memória (skill `knowledge_capture`).
5. **Modo manhã**: acrescente o panorama de hoje — pendências herdadas, prazos de hoje, compromissos
   registrados — e sugira o foco do dia (o plano detalhado é da skill `daily_planning`).
   **Modo noite**: proponha 1 ajuste para amanhã e pergunte, sem insistir, como foi a energia do dia.
6. **Ações**: tarefas novas que surgiram → `kanban_create(assignee=<domínio>, tenant=<tenant>,
   title=..., idempotency_key="review-<AAAA-MM-DD>-<slug>")`; pendências que já têm tarefa → comente
   com `kanban_comment` em vez de duplicar.
7. **Registrar** a revisão: `mcp__knowledge__memory_save(tenant=<tenant>, scope="domain",
   domain="chief", kind="episode", lifecycle="temporary", importance=0.4,
   content="<AAAA-MM-DD> revisão: feito …; pendente amanhã …; aprendizado …")`. Aprendizado durável →
   memória separada `kind="working_style"` ou `"fact"`, `lifecycle="important"`.

## Outputs

```markdown
## Revisão <manhã|noite> — <data> (<tenant>)
**Feito:** … · **Parcial/não feito:** … (motivo)
**Amanhã/hoje:** 1) … 2) …
**Bloqueios seus:** …
**Aprendizado:** …
**Ajuste sugerido:** …
```

## Gate e escalonamento

- Não há validador automático para revisão diária (o gate `aios` só roda quando código é editado): a evidência
  é cada item apontar para uma fonte (tarefa Kanban, memória, nota) — sem fonte, "sem dados".
- Nunca peça outro modelo. Bloqueio "[aios] … tenant" ou "orçamento esgotado": não chame mais
  ferramentas nem troque de tenant; entregue o que já tem e diga o que faltou.
- Leituras independentes numa única rodada de ferramentas (em paralelo); gravações (`kanban_create`,
  `memory_save`) antes de escrever a entrega final.

## Pitfalls

- Julgar o usuário ("você procrastinou"): descreva fatos e proponha ajuste, sem moralizar.
- Reportar como feito o que só tem tarefa criada; confundir tenant.
- Revisão longa demais: se passar de ~15 linhas, corte.

## Verification

- Cada item de "Feito" corresponde a uma tarefa concluída ou registro do dia; a memória `episode`
  do dia foi gravada (ou o motivo de não gravar foi dito).
