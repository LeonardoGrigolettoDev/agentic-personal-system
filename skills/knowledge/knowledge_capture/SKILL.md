---
name: knowledge_capture
description: "Salvar conhecimento durável: notas e memórias por tenant."
version: 1.0.0
author: Leonardo Grigoletto (agent-system)
license: Proprietary
platforms: [linux, macos]
metadata:
  hermes:
    category: knowledge
    tags: [knowledge, memory, notes, tenants, learning]
    related_skills: [memory_hygiene, decision_record, transcript_to_notes, source_synthesis]
---

# Knowledge Capture

Decide o que de uma conversa, tarefa, aula ou pesquisa merece ser lembrado e grava no lugar certo:
memória curta (`memory_save`) para fatos que mudam decisões futuras, documento (`ingest_note`) para
conteúdo longo, ou nada. Mantém o isolamento entre tenants e evita duplicatas. O modelo completo
(tenants, scopes, kinds, lifecycles) está em `references/memory-model.md`.

## When to Use

- Fim de tarefa/sessão com aprendizado, preferência, regra ou fato novo.
- Revisão de estudos (20:00): consolidar o que foi estudado no dia.
- O usuário diz "lembre que…", "anota isso", "salva esse resumo".

Não use para decisões formais (use `decision_record`) nem para limpar memórias (`memory_hygiene`).

## Inputs

- Material: conversa atual, resultado de tarefa, notas de aula, resumo de pesquisa ou transcrição.
- Tenant da sessão (`nitro` | `pessoal` | `shared`), domínio e projeto, quando houver.

## Procedure

1. **Triagem** — liste candidatos e classifique cada um (tabela "O que vira o quê" da referência):
   memória · documento · nada. Descarte: o que já está na base, conversa trivial, dados brutos
   (extratos, logs), segredos, opiniões passageiras.
2. **Tenant e escopo**: tenant = o da sessão; `shared` só para preferências realmente globais (ex.:
   estilo de comunicação). Escopo: `project` se o fato só vale para um projeto cadastrado; `domain`
   se vale para um domínio; `global` se vale para tudo no tenant. Fato de projeto: comece o texto com
   `[projeto <slug>]` (é o que o acha quando o projeto não está cadastrado).
3. **Dedupe**: para cada memória candidata, `mcp__knowledge__memory_search(tenant=<tenant>,
   query="<tema>", kind=<kind>)` — para projeto, `query="[projeto <slug>] <tema>"` sem filtro
   `project`. Já existe igual → não salve. Existe desatualizada → salve a versão corrigida (a
   substituição é automática por similaridade) e mencione que corrige a anterior.
4. **Escrever** a memória: uma frase ou poucas, autocontida, datada se o tempo importa, terceira pessoa.
   Ex.: `"O usuário estuda Go às terças e quintas, 19h–21h; prefere exercícios a teoria (2026-10)."`
5. **Gravar**:
   - Memória: `mcp__knowledge__memory_save(tenant=<tenant>, scope=<scope>, kind=<kind>, content=<texto>,
     lifecycle=<temporary|important|persistent>, domain=<domínio>, project=<slug>, importance=<0–1>)`.
     Erro "unknown project" → repita com `scope="domain"` e prefixo `[projeto <slug>]` no texto.
   - Documento: `mcp__knowledge__ingest_note(tenant=<tenant>, title="<Tipo>: <tema> (<AAAA-MM-DD>)",
     content=<markdown>, domain=<domínio>, source_uri="agent://notes/<domínio>/<slug-do-tema>")`.
     O título não deduplica: sem `source_uri` o URI vem do hash de título + conteúdo, e reenviar a nota
     corrigida cria um segundo documento vivo. Mesmo `source_uri` = o documento é atualizado.
6. **Estudos (domínio `learning`)**: para cada tema do dia, uma nota que é o registro datado da sessão:
   `ingest_note(..., title="Estudo: <tema> (<AAAA-MM-DD>)", domain="learning",
   source_uri="agent://learning/<AAAA-MM-DD>/<slug-do-tema>")`, com conteúdo começando por
   `# Estudo: <tema> — <AAAA-MM-DD>` e trazendo resumo, 3–5 conceitos-chave e 3 perguntas de revisão
   (com resposta). URI único por data e tema: nunca é substituída, e a revisão espaçada a acha pela data
   (`knowledge_search` casa a data literal). Mais uma memória `kind="episode"`, `lifecycle="important"`,
   `content="<AAAA-MM-DD>: estudou <tema> (<fonte>)"` — uma sessão nova do mesmo tema pode substituí-la
   (fica a mais recente; o histórico está nas notas). Metas de estudo alteradas → `kind="roadmap"`,
   `lifecycle="persistent"` (com aprovação).
7. **Confirmar** ao usuário em uma linha por item: o que foi salvo, onde (tenant/scope/kind) e o
   resultado (`created` | `superseded` | `duplicate`).

## Outputs

```markdown
Salvo:
- memória pessoal/learning/episode (important): "2026-10-07: estudou canais em Go…" → created
- nota pessoal/learning: "Estudo: canais em Go (2026-10-07)" → ingerida
Não salvo: <itens descartados e por quê>
```

## Gate e escalonamento

- Não há validador automático para captura (o gate `aios` só roda quando código é editado): a
  evidência é a confirmação por item (tenant/scope/kind e `created|superseded|duplicate`).
- Nunca peça outro modelo. Bloqueio "[aios] … tenant" ou "orçamento esgotado": não insista nem troque
  de tenant; liste o que ficou sem gravar.

## Pitfalls

- Salvar tudo: a memória vira ruído e o Context Compiler gasta orçamento com lixo. Na dúvida, não salve.
- Tenant errado (trabalho em `pessoal`, ou dados pessoais em `shared`): o plugin pode bloquear, mas
  a responsabilidade é sua — confira antes de gravar.
- Memória que depende da conversa para fazer sentido ("isso que falamos"); conteúdo > 4000 caracteres.
- `memory_search` ordena por similaridade, não por data: para "o que aconteceu no dia X" use notas
  datadas (`knowledge_search` com a data) ou filtre `domain`/`kind` com `k` alto e leia as datas.
- Nunca grave segredos, senhas, tokens, números de documento ou dados de terceiros sem necessidade.

## Verification

- `mcp__knowledge__memory_search(tenant=<tenant>, query="<tema>")` devolve a memória nova no topo
  e não há duas memórias vivas dizendo coisas diferentes sobre o mesmo tema.
