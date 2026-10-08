---
name: roadmap_update
description: "Atualizar roadmap de projeto com base em evidências."
version: 1.0.0
author: Leonardo Grigoletto (agent-system)
license: Proprietary
platforms: [linux, macos]
metadata:
  hermes:
    category: projects
    tags: [projects, roadmap, planning, milestones]
    related_skills: [project_status, decision_record, weekly_review]
---

# Roadmap Update

Mantém o roadmap de um projeto honesto: marca marcos concluídos, re-planeja os ameaçados, encaixa
trabalho novo e corta o que perdeu sentido — sempre a partir do status atual e das decisões
registradas. Propõe mudanças; só grava o que o usuário aprovar.

## When to Use

- Após `project_status` mostrar marco em risco/atrasado, ou quando surgir escopo novo.
- Revisão de projetos (18:30) e revisão semanal; "replaneje o projeto X", "o que entra no próximo mês?".

## Inputs

- Projeto (slug) e tenant (`nitro` trabalho | `pessoal` projetos pessoais).
- Roadmap atual: memórias `kind="roadmap"` do projeto e/ou nota/arquivo de roadmap indicado pelo usuário.
- Status recente (saída de `project_status`) e capacidade disponível (horas/semana), se conhecida.

## Procedure

1. **Carregar o roadmap vigente**: `mcp__knowledge__memory_search(tenant=<tenant>, kind="roadmap",
   query="[projeto <slug>] roadmap marcos")` e `mcp__knowledge__knowledge_search(tenant=<tenant>,
   query="roadmap <projeto>", domain="projects")`. Não filtre por `project`: memórias de projeto não
   cadastrado ficam em `scope="domain"` com o prefixo `[projeto <slug>]` (passo 7) e o filtro as
   esconderia. Só sem nenhum resultado proponha um roadmap inicial com 3–5 marcos.
2. **Status atual**: use o relatório de `project_status` desta sessão ou gere-o agora.
3. **Decisões que afetam escopo**: `mcp__knowledge__memory_search(tenant=<tenant>, kind="decision",
   query="[projeto <slug>] escopo prioridade")`.
4. **Diferença**: para cada marco → `concluído` | `no prazo` | `em risco` | `atrasado` | `obsoleto`,
   com a evidência. Liste trabalho novo ainda sem marco.
5. **Proposta** (diff do roadmap): mover datas com motivo, dividir marcos grandes, cortar/adiar itens,
   encaixar novos itens por valor × esforço. Capacidade é limite: não empilhe tudo no próximo mês.
6. **Aprovação**: apresente o diff e pergunte. Sem aprovação explícita, não grave nada.
7. **Gravar o aprovado**:
   - `mcp__knowledge__memory_save(tenant=<tenant>, scope="project", project=<slug>, domain="projects",
     kind="roadmap", lifecycle="persistent", content="[projeto <slug>] Roadmap <AAAA-MM-DD>: M1 … (data)
     · M2 …")` — memórias quase idênticas substituem a anterior automaticamente (o histórico fica).
     Erro "unknown project" (projeto não cadastrado) → repita com `scope="domain"` e o mesmo `content`.
   - Mudança de rumo relevante → também `decision_record`.
   - Trabalho do próximo marco vira tarefas: `kanban_create(assignee=<domínio>, tenant=<tenant>, ...)`.

## Outputs

```markdown
## Roadmap <projeto> — proposta de <data>
| Marco | Antes | Proposta | Motivo/evidência |
| M2 API pública | 15/out | 29/out | bloqueio de auth (tarefa t_ab12), 1 semana sem progresso |
| M4 Relatórios | — | novo, nov | pedido do cliente (nota 03/out) |
**Cortes/adiamentos:** … **Capacidade assumida:** … h/semana
**Precisa de você:** aprovar o diff acima
```

## Gate e escalonamento

- Não há validador automático para roadmap (o gate `aios` só roda quando código é editado): a
  evidência é a tabela antes × proposta com o motivo de cada mudança e a aprovação explícita do usuário.
- Nunca peça outro modelo. Ao ver "orçamento esgotado", não chame mais ferramentas: entregue o diff
  proposto como texto, marcado como não gravado.
- Sem status confiável para um marco, diga "sem dados" naquele marco em vez de mover a data.

## Pitfalls

- Re-planejar sem mudar nada além das datas (o mesmo plano atrasa de novo): ajuste escopo também.
- Gravar roadmap sem aprovação; salvar várias versões quase iguais a cada execução agendada.
- Misturar roadmaps de projetos `nitro` e `pessoal`.

## Verification

- O roadmap gravado é exatamente o aprovado, com data; cada mudança de data tem motivo e evidência.
