---
name: decision_record
description: "Registrar decisão (ADR): contexto, opções, escolha, motivo."
version: 1.0.0
author: Leonardo Grigoletto (agent-system)
license: Proprietary
platforms: [linux, macos]
metadata:
  hermes:
    category: projects
    tags: [projects, adr, decisions, architecture, memory]
    related_skills: [architecture_review, roadmap_update, knowledge_capture]
---

# Decision Record

Registra uma decisão já tomada (técnica, de produto, financeira ou pessoal) no formato ADR curto e a
torna recuperável: documento na base de conhecimento + memória `kind="decision"` que o Context
Compiler injeta em tarefas futuras. Também marca decisões antigas como substituídas.

## When to Use

- O usuário decidiu algo que deve guiar trabalho futuro ("vamos usar Postgres", "orçamento de lazer
  passa a R$ 400", "não aceitar projetos freelance em dezembro").
- Ao fim de `architecture_review` ou `roadmap_update` com decisão aprovada.

Não registre preferências triviais ou passageiras (use `knowledge_capture` com `lifecycle="temporary"`
ou nada).

## Inputs

- A decisão, o motivo, as alternativas consideradas e quem decidiu; data.
- Tenant (`nitro` | `pessoal` | `shared` para regras globais), domínio e projeto (se houver).

## Procedure

1. **Verificar duplicata/conflito**: `mcp__knowledge__memory_search(tenant=<tenant>, kind="decision",
   query="[projeto <slug>] <tema da decisão>")` — sem o filtro `project`, que não acha memórias gravadas
   pelo fallback do passo 5. Se existe decisão vigente sobre o mesmo tema, esta a substitui: cite-a em
   "Substitui".
2. **Preencher o ADR** com `templates/adr.md` (curto: uma tela). Confirme com o usuário qualquer campo
   inferido (principalmente motivo e alternativas).
3. **Numerar**: `ADR-<AAAA-MM-DD>-<slug-curto>` (estável, sem depender de contador global).
4. **Guardar o documento**: `mcp__knowledge__ingest_note(tenant=<tenant>, title="ADR <id>: <título>",
   content=<ADR em markdown>, domain=<domínio>, source_uri="agent://adr/<projeto|geral>/<id>")` — URI
   estável: reenviar o ADR corrigido atualiza o mesmo documento.
   Se o projeto tem repositório e a decisão é técnica, proponha também `docs/adr/<id>.md` no repo
   (via tarefa para `engineering`, com aprovação).
5. **Memória curta para o Context Compiler**: `mcp__knowledge__memory_save(tenant=<tenant>,
   scope="project" (ou "domain"/"global"), project=<slug>, domain=<domínio>, kind="decision"
   (ou "architecture" para estrutura técnica), lifecycle="persistent", importance=0.8,
   content="[projeto <slug>] <AAAA-MM-DD> <decisão em 1–2 frases> — motivo: <...> (ADR <id>)")`.
   Erro "unknown project" (projeto não cadastrado no knowledge) → repita com `scope="domain"` e o
   mesmo `content` (o prefixo `[projeto <slug>]` mantém a busca do passo 1 funcionando).
6. **Consequências acionáveis** (migrar algo, atualizar docs) viram tarefas
   `kanban_create(assignee=<domínio>, tenant=<tenant>, title=..., body="Consequência do ADR <id>: ...")`.

## Outputs

- ADR em markdown (template) + confirmação: `ADR <id> salvo (doc + memória decision, tenant <tenant>)`,
  e a lista de tarefas criadas.

## Gate e escalonamento

- Não há validador automático para registro de decisão (o gate `aios` só roda quando código é
  editado): a evidência é o ADR confirmado pelo usuário e a decisão anterior citada em "Substitui".
- Nunca peça outro modelo. Ao ver "orçamento esgotado", não chame mais ferramentas: entregue o ADR em
  texto e diga o que ficou sem gravar.
- Motivo ou alternativas incertos numa decisão de alto impacto: pergunte antes de gravar (ou
  `kanban_block` com o motivo, se for tarefa Kanban).

## Pitfalls

- Registrar como decisão algo que ainda é proposta: pergunte "isso está decidido?".
- Esquecer "Substitui": duas decisões vigentes contraditórias confundem o Context Compiler.
- Decisão de trabalho (`nitro`) salva em `pessoal` ou vice-versa; `shared` só para regras realmente globais.

## Verification

- `mcp__knowledge__memory_search(tenant=<tenant>, kind="decision", query="<tema>")` devolve a nova
  decisão no topo, e `mcp__knowledge__knowledge_search(tenant=<tenant>, query="ADR <id>")` encontra o
  documento (`ingest_note` respondeu sem erro).
