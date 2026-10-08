---
name: project_status
description: "Status de projeto: feito, em curso, riscos, próximos passos."
version: 1.0.0
author: Leonardo Grigoletto (agent-system)
license: Proprietary
platforms: [linux, macos]
metadata:
  hermes:
    category: projects
    tags: [projects, status, kanban, reporting]
    related_skills: [roadmap_update, decision_record, repository_analysis, weekly_review]
---

# Project Status

Relatório de status de um ou mais projetos montado a partir de evidências: tarefas no Kanban,
memórias e notas do projeto, decisões registradas e (para projetos de código) atividade no
repositório. Separa fato de inferência e termina em próximos passos acionáveis com dono.

## When to Use

- "Como está o projeto X?", revisão de engenharia (09:00) ou de projetos (18:30), preparação de reunião.
- Antes de `roadmap_update` (o status é a entrada do roadmap).

## Inputs

- Projeto(s): slug/nome; sem nome, todos os projetos ativos do tenant.
- Tenant: `nitro` para projetos do trabalho, `pessoal` para projetos pessoais. Nunca combine os dois
  num mesmo relatório; para os dois, rode duas vezes e entregue seções separadas.
- Janela: padrão últimos 7 dias (diário: últimas 24 h).

## Procedure

Chame as leituras dos passos 1–3 numa única rodada de ferramentas (em paralelo): poupa orçamento.

1. **Contexto compilado**: `mcp__knowledge__compile_context(task="status do projeto <slug>",
   tenant=<tenant>, domain="projects", project=<slug>)` — descrição, repositório, decisões, restrições,
   roadmap. Projeto não cadastrado no knowledge: o campo `project` vem vazio; siga com as memórias.
2. **Memórias do projeto**: `mcp__knowledge__memory_search(tenant=<tenant>, query="[projeto <slug>]
   status bloqueio prazo")` e o mesmo com `kind="roadmap"`/`kind="issue"` para marcos e problemas
   abertos. Não filtre por `project`: memórias de projeto não cadastrado ficam em `scope="domain"` com
   o prefixo `[projeto <slug>]` e o filtro as esconderia.
3. **Tarefas**: `kanban_list(tenant=<tenant>)` (filtre por `assignee`/`status` quando útil). Classifique:
   concluídas na janela, em andamento, bloqueadas (motivo), paradas há > 3 dias sem atualização.
   Como worker Kanban (tarefa despachada para `projects`), `kanban_list` não existe: use a lista de
   tarefas que veio no `body` da sua tarefa (`kanban_show`) e, para detalhes de uma tarefa citada,
   `kanban_show(task_id=<id>)`. Sem lista no body, diga "tarefas: sem dados" em vez de inventar.
4. **Código (se o projeto tem repositório)**: este agente pode não ter shell. Peça evidência ao agente
   de engenharia em vez de supor, com a URL do repositório (campo `repository` de
   `mcp__knowledge__project_list(tenant=<tenant>)` ou do passo 1, ou memória `[projeto <slug>] repositório: <url>`): `kanban_create(title="Mapa de atividade: <repo>",
   assignee="engineering", tenant=<tenant>, skills=["repository_analysis"],
   idempotency_key="status-<slug>-<AAAA-MM-DD>", body="repo: <git-url> ref: <branch>. Rodar
   aios-task-start <id-da-tarefa> <git-url> <branch>, repo_map.sh e aios-check; devolver commits da
   janela, testes (linha de base), marcadores pendentes. Não alterar código.")`. URL desconhecida: não
   crie a tarefa; peça a URL em "Precisa de você". Se você tem `terminal`, rode a skill
   `repository_analysis` diretamente.
5. **Avaliar**: compare o andamento com o próximo marco do roadmap. Status geral:
   `no prazo` | `em risco` (marco ameaçado, bloqueio sem dono) | `atrasado` (marco vencido).
6. **Próximos passos**: no máximo 5, cada um com dono (usuário ou agente), prazo e critério de pronto.
   Trabalho delegável vira tarefa: `kanban_create(assignee=<engineering|projects|...>, tenant=<tenant>,
   title=..., body=<critério de pronto>)`. Decisões pendentes vão para a lista "precisa do usuário".
7. **Registrar**: status relevante e duradouro (marco atingido, risco novo) →
   `mcp__knowledge__memory_save(tenant=<tenant>, scope="project", project=<slug>, domain="projects",
   kind="roadmap" ou "issue", lifecycle="important", content="[projeto <slug>] <AAAA-MM-DD>: ...")`.
   Erro "unknown project" → repita com `scope="domain"` e o mesmo `content`. URL de repositório
   descoberta → `kind="fact"`, `content="[projeto <slug>] repositório: <git-url> (branch <b>)"`.

## Outputs

```markdown
## <Projeto> — <no prazo | em risco | atrasado> (<tenant>, <data>)
**Feito (7d):** …  **Em andamento:** …  **Bloqueado:** … (motivo, desde)
**Próximo marco:** <marco> em <data> — <confiança>
**Riscos:** …
**Próximos passos:** 1) <ação> — <dono> — <prazo>
**Precisa de você:** <decisões/aprovações>
```

## Gate e escalonamento

- Tipo `project_management`, sem validador automático (o gate `aios` só roda quando código é
  editado): a evidência é o que sustenta o status. Nunca peça outro modelo; se faltar evidência, diga
  "sem dados" em vez de inventar progresso.
- Ao ver "orçamento esgotado", não chame mais ferramentas: entregue o status parcial com o que já tem.

## Pitfalls

- Confundir atividade com progresso (muitos commits ≠ marco mais perto).
- Tarefas Kanban de outro tenant no relatório: sempre filtre por `tenant`.
- Criar tarefas duplicadas em execuções agendadas: use `idempotency_key` com data.

## Verification

- Cada item de "Feito/Bloqueado" aponta para uma tarefa Kanban, nota ou commit; o status geral
  é coerente com o próximo marco.
