---
name: memory_hygiene
description: "Limpar memórias duplicadas, obsoletas ou conflitantes."
version: 1.0.0
author: Leonardo Grigoletto (agent-system)
license: Proprietary
platforms: [linux, macos]
metadata:
  hermes:
    category: knowledge
    tags: [knowledge, memory, maintenance, quality]
    related_skills: [knowledge_capture, decision_record, weekly_review]
---

# Memory Hygiene

Audita as memórias de um tenant e propõe correções: duplicatas, contradições, fatos vencidos,
memórias no escopo/tenant errado e episódios que deveriam ser temporários. Corrige o que dá para
corrigir gravando versões novas (a substituição por similaridade é automática) e lista as que precisam
ser descontinuadas para o usuário aprovar. Nada é apagado sem aprovação.

## When to Use

- Revisão semanal, ou quando o contexto injetado trouxer informação errada/antiga.
- "Revise minhas memórias de X", "o agente acha que eu ainda uso Y".

## Inputs

- Tenant (`nitro` | `pessoal` | `shared`) — uma auditoria por tenant; domínio/projeto opcionais.
- Tema suspeito, se houver (ex.: "orçamento", "stack do mini-ERP").

## Procedure

1. **Amostrar por tema e tipo** (cada chamada com o tenant):
   `mcp__knowledge__memory_search(tenant=<tenant>, query="<tema>", kind=<kind>, k=50)` para os kinds
   relevantes (`decision`, `rule`, `preference`, `roadmap`, `fact`, `architecture`) e, se houver, por
   `domain`/`project`. Sem tema, varra por domínio com consultas genéricas ("preferências", "regras",
   "decisões", "metas", "status").
2. **Classificar** cada memória suspeita:
   - `duplicada`: mesmo fato com outras palavras (a mais completa/recente fica);
   - `conflitante`: afirmações incompatíveis vivas (vale a mais recente **ou** a de um ADR; na dúvida, pergunte);
   - `vencida`: datada e passada (plano de semana antiga, status antigo) ou contradita por evidência atual;
   - `lugar errado`: tenant/escopo/kind inadequado (ex.: dado de trabalho em `pessoal`, episódio `persistent`);
   - `ok`.
3. **Plano** em tabela (id curto, conteúdo resumido, classe, ação proposta). Ações possíveis:
   - **corrigir/consolidar**: gravar uma memória nova com o texto certo, mesmo tenant/scope/kind
     (`mcp__knowledge__memory_save(...)`) — se a similaridade for alta, ela substitui a antiga;
   - **mover de escopo/kind** (mesmo tenant): gravar no escopo/kind certo e descontinuar a original;
   - **mover de tenant**: impossível nesta sessão — o plugin `aios` bloqueia `memory_save` em tenant
     diferente do da sessão (exceto `shared`). Proponha uma tarefa no tenant de destino
     (`kanban_create(tenant=<destino>, assignee=<domínio>, title="Gravar memória movida", body="<texto>")`,
     criada pelo usuário ou por uma sessão desse tenant) e descontinue a original só depois;
   - **descontinuar**: memórias sem substituta.
4. **Aprovação**: mostre o plano e espere "ok" explícito. Itens sensíveis (decisões, regras) sempre
   exigem confirmação individual.
5. **Executar o aprovado**: gravações via `memory_save`; para descontinuar, o agente não tem ferramenta
   MCP — entregue ao usuário o comando para rodar no host e registre o pedido:
   ```bash
   make skills-memory-deprecate tenant=<tenant> ids="<uuid1> <uuid2>"
   make kb-maintain        # expira temporárias vencidas e substituídas
   ```
6. **Registrar o resultado**: `mcp__knowledge__memory_save(tenant=<tenant>, scope="domain", domain="chief",
   kind="episode", lifecycle="temporary", content="<data>: higiene de memória — N corrigidas, M a descontinuar")`.

## Outputs

```markdown
## Higiene de memórias — <tenant> (<data>)
| id | memória | classe | ação |
| 3f2a… | "Orçamento de lazer R$ 300" (2026-03) | vencida (ADR 2026-09: R$ 400) | descontinuar |
Gravadas: 2 (1 superseded) · Para descontinuar (rode no host): `make skills-memory-deprecate tenant=pessoal ids="3f2a…"`
```

## Gate e escalonamento

- Não há validador automático para higiene de memória (o gate `aios` só roda quando código é editado):
  a evidência é a tabela com a classe e o motivo de cada item, aprovada pelo usuário.
- Nunca peça outro modelo. Bloqueio por tenant ou "orçamento esgotado": pare, entregue o plano com o que
  foi executado e o que falta.

## Pitfalls

- Apagar memória porque "parece velha" sem evidência de que mudou.
- Consolidar memórias de tenants diferentes numa só: isolamento vale também aqui.
- Episódios recentes não são lixo: a revisão semanal depende deles; só os antigos devem expirar.

## Verification

- Após a execução, `memory_search` pelo mesmo tema não devolve mais contradições vivas, e as
  memórias corrigidas aparecem com o texto novo.
