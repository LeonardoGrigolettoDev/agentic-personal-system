---
name: weekly_review
description: "Revisão semanal: metas, projetos parados e plano da semana."
version: 1.0.0
author: Leonardo Grigoletto (agent-system)
license: Proprietary
platforms: [linux, macos]
metadata:
  hermes:
    category: productivity
    tags: [productivity, review, weekly, goals, planning]
    related_skills: [daily_review, daily_planning, project_status, budget_review, memory_hygiene]
---

# Weekly Review

Reinício semanal de um tenant: o que foi concluído, o que escorregou, projetos sem próximo passo,
saúde financeira e de estudos da semana, e um plano da próxima semana com 3–5 resultados que cabem na
capacidade real. Usa os episódios diários gravados pelas revisões e o Kanban como fonte; delega
análises de domínio aos agentes certos em vez de fazê-las aqui.

## When to Use

- Execução agendada "weekly-review" (domingo, 19:00) ou "faça minha revisão semanal".

## Inputs

- Semana de referência (padrão: segunda a domingo encerrando hoje) e tenant da execução (`pessoal` | `nitro`).
- Metas vigentes (memórias `kind="roadmap"`) e capacidade da próxima semana, se conhecida.

## Procedure

1. **Evidências da semana** (tenant da sessão em todas as chamadas):
   - Episódios: `mcp__knowledge__memory_search(tenant=<tenant>, query="revisão plano dia semana",
     domain="chief", kind="episode", k=60)` — planos e revisões diárias (temporárias, 14 dias). A busca
     ordena por similaridade, não por data: com o filtro e `k` alto vêm todas as recentes; use as que
     começam com uma data da semana.
   - Tarefas: `kanban_list(tenant=<tenant>)` — concluídas na semana, bloqueadas, paradas > 5 dias.
   - Metas: `mcp__knowledge__memory_search(tenant=<tenant>, query="metas objetivos semana mês",
     kind="roadmap")`.
   - Decisões novas: `mcp__knowledge__memory_search(tenant=<tenant>, query="decisão", kind="decision", k=10)`.
2. **Vitórias e escorregões**: concluído vs. planejado; o que foi carregado de semana em semana
   (mesma pendência ≥ 2 semanas = decidir: fazer, delegar, adiar com data ou abandonar).
3. **Projetos**: para cada projeto ativo do tenant, há próximo passo com dono? Sem próximo passo →
   "parado". Para status detalhado, delegue: `kanban_create(assignee="projects", tenant=<tenant>,
   skills=["project_status"], title="Status semanal: <projeto>", idempotency_key="weekly-<AAAA-Www>-<slug>",
   body="Projeto <slug>. Tarefas: <id> <título> <status> <dias parado>; …")` — o worker não tem
   `kanban_list`, então a lista de tarefas do projeto vai no `body`.
4. **Domínios**: finanças → `kanban_create(assignee="finance", tenant=<tenant>, skills=["budget_review"],
   title="Orçamento: fechamento parcial <mês>", idempotency_key="weekly-<AAAA-Www>-budget")`;
   estudos (tenant `pessoal`) → `mcp__knowledge__memory_search(tenant="pessoal", query="estudou",
   domain="learning", kind="episode", k=50)` e as datas da semana no texto de cada memória.
5. **Plano da próxima semana**: 3–5 resultados (não tarefas soltas), cada um ligado a uma meta;
   capacidade: compromissos fixos conhecidos + margem de 30%. Liste o que foi conscientemente adiado.
6. **Manutenção**: se notar memórias contraditórias ou vencidas durante a revisão, sugira a skill
   `memory_hygiene` (não execute limpeza aqui sem pedido).
7. **Registrar**:
   - Resumo da semana: `mcp__knowledge__memory_save(tenant=<tenant>, scope="domain", domain="chief",
     kind="episode", lifecycle="important", importance=0.5, content="Semana <AAAA-Www>: concluído …;
     escorregou …; plano próxima: R1 … R2 …")`.
   - Metas alteradas (com aprovação do usuário): `kind="roadmap"`, `lifecycle="persistent"`.

## Outputs

```markdown
## Revisão semanal <AAAA-Www> (<tenant>)
**Vitórias:** … **Escorregou:** … (padrão observado)
**Projetos:** <projeto> — ok | parado (sem próximo passo) | em risco
**Finanças / Estudos:** <resumo de 1 linha ou "análise delegada (t_…)">
**Próxima semana (resultados):** 1) … 2) … 3) …
**Adiado conscientemente:** …
**Decisões para você:** …
```

## Gate e escalonamento

- Não há validador automático para revisão semanal (o gate `aios` só roda quando código é editado): a evidência
  é cada item apontar para uma fonte (tarefa Kanban, memória, nota) — sem fonte, "sem dados".
- Nunca peça outro modelo. Bloqueio "[aios] … tenant" ou "orçamento esgotado": não chame mais
  ferramentas nem troque de tenant; entregue o que já tem e diga o que faltou.
- Leituras independentes numa única rodada de ferramentas (em paralelo); gravações (`kanban_create`,
  `memory_save`) antes de escrever a entrega final.

## Pitfalls

- Virar lista de tudo o que existe: a revisão serve para escolher o que NÃO fazer também.
- Planejar a semana sem olhar a capacidade; carregar automaticamente todo pendente.
- Misturar `nitro` e `pessoal` na mesma revisão: uma execução por tenant.

## Verification

- Cada resultado da próxima semana se liga a uma meta ou pendência citada, e o resumo `episode` da
  semana foi gravado.
