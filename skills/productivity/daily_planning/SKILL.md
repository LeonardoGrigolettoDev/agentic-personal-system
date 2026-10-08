---
name: daily_planning
description: "Planejar o dia: prioridades, blocos de tempo e delegação."
version: 1.0.0
author: Leonardo Grigoletto (agent-system)
license: Proprietary
platforms: [linux, macos]
metadata:
  hermes:
    category: productivity
    tags: [productivity, planning, daily, kanban, priorities]
    related_skills: [daily_review, weekly_review, project_status]
---

# Daily Planning

Monta o plano do dia de um tenant a partir de evidências — pendências de ontem, tarefas no Kanban,
compromissos registrados, metas da semana e preferências de trabalho — e entrega no máximo 3
prioridades, blocos de tempo realistas e o que será delegado aos agentes de domínio. Propõe; não
reagenda compromissos nem envia mensagens a terceiros.

## When to Use

- Execução agendada "daily-planning" (dias úteis, 08:00) ou "o que eu faço hoje?", "planeje meu dia".

## Inputs

- Data de hoje (da sessão) e tenant da execução: `nitro` (dia de trabalho) ou `pessoal`.
- Opcional: horas disponíveis, compromissos fixos informados pelo usuário.

## Procedure

1. **Coletar** (todas as chamadas com o tenant da sessão; `shared` só para preferências):
   - Revisão de ontem e pendências: `mcp__knowledge__memory_search(tenant=<tenant>, query="<data de
     ontem> revisão noite pendências amanhã", domain="chief", kind="episode", k=10)` — use só as que
     começam com a data de ontem (a busca ordena por similaridade, não por data).
   - Metas da semana: `mcp__knowledge__memory_search(tenant=<tenant>, query="plano da semana metas",
     kind="roadmap")`.
   - Preferências de trabalho (horários de foco, limites): `mcp__knowledge__memory_search(tenant=<tenant>,
     query="preferências rotina horário foco", kind="working_style")` (inclui `shared`).
   - Compromissos do dia registrados em notas: `mcp__knowledge__knowledge_search(tenant=<tenant>,
     query="agenda compromissos <data por extenso>", k=5)`. Sem fonte de agenda, diga "agenda: sem dados".
   - Tarefas: `kanban_list(tenant=<tenant>)` — abertas, bloqueadas, com prazo hoje/vencido.
2. **Candidatos**: junte pendências de ontem, prazos de hoje/vencidos, itens bloqueados que dependem
   do usuário e passos das metas da semana. Remova duplicatas.
3. **Priorizar** (determinístico antes de opinião): 1) prazo hoje/vencido com consequência; 2)
   desbloqueia outras pessoas/agentes; 3) avança meta da semana; 4) resto. No máximo 3 prioridades.
4. **Capacidade**: some os compromissos fixos; planeje no máximo ~70% das horas livres (reserve
   imprevistos). Blocos de foco nos horários preferidos. O que não cabe vai para "não hoje" com motivo.
5. **Delegar** o que um agente executa sozinho: `kanban_create(title=..., assignee=<engineering|finance|
   projects|personal|learning>, tenant=<tenant>, body="<objetivo + critério de pronto>",
   idempotency_key="plan-<AAAA-MM-DD>-<slug>")`. Sem ferramentas `kanban_*` na sessão, liste as
   delegações propostas no plano.
6. **Registrar** o plano (curto) para a revisão da noite:
   `mcp__knowledge__memory_save(tenant=<tenant>, scope="domain", domain="chief", kind="episode",
   lifecycle="temporary", importance=0.4, content="<AAAA-MM-DD> plano: P1 …; P2 …; P3 …; delegado: …")`.

## Outputs

```markdown
## Plano de <dia, data> (<tenant>)
**Prioridades:** 1) … (por quê) 2) … 3) …
**Agenda:** 09:00–11:00 foco: P1 · 14:00 reunião X · …
**Delegado:** t_ab12 engineering — …
**Bloqueios que dependem de você:** …
**Não hoje:** … (motivo)
```

## Gate e escalonamento

- Não há validador automático para planejamento (o gate `aios` só roda quando código é editado): a evidência
  é cada item apontar para uma fonte (tarefa Kanban, memória, nota) — sem fonte, "sem dados".
- Nunca peça outro modelo. Bloqueio "[aios] … tenant" ou "orçamento esgotado": não chame mais
  ferramentas nem troque de tenant; entregue o que já tem e diga o que faltou.
- Leituras independentes numa única rodada de ferramentas (em paralelo); gravações (`kanban_create`,
  `memory_save`) antes de escrever a entrega final.

## Pitfalls

- Lista de 12 "prioridades": se tudo é prioridade, nada é. Máximo 3.
- Planejar 100% do dia; ignorar compromissos fixos.
- Inventar compromissos que não estão em nenhuma fonte; misturar itens de outro tenant.
- Tarefas duplicadas a cada execução: sempre `idempotency_key` com data.

## Verification

- Cada prioridade aponta para uma fonte (tarefa, pendência, meta) e o total planejado cabe na
  capacidade declarada.
