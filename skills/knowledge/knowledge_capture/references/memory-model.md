# Modelo de conhecimento do AI Agent OS (knowledge service, MCP `knowledge`)

## Tenants (isolamento — nunca misture)
| Tenant | Conteúdo | Quem acessa |
|---|---|---|
| `nitro` | trabalho na empresa Nitro: apps, clientes, código, projetos do trabalho | chief, engineering, finance, projects |
| `pessoal` | vida pessoal: estudos, finanças pessoais, projetos pessoais, rotina, saúde | todos os agentes |
| `shared` | preferências e regras globais, sem dado de trabalho ou pessoal específico | leitura por todos |

- Uma sessão = um tenant. O plugin `aios` bloqueia chamadas com `tenant` diferente do da sessão
  (exceto `shared`). Bloqueio por isolamento não se contorna: reporte e siga.
- Passe `tenant` explicitamente em toda ferramenta de conhecimento e em `kanban_create`.

## Ferramentas MCP (prefixo `mcp__knowledge__`)
| Ferramenta | Argumentos | Uso |
|---|---|---|
| `knowledge_search` | `tenant, query, domain?, k=8, include_shared=false` | busca híbrida em documentos/notas |
| `compile_context` | `task, tenant, domain?, project?, budget_tokens=3000` | contexto enxuto: regras > decisões > projeto > memórias > docs |
| `memory_search` | `tenant, query, scope?, domain?, project?, kind?, k=10` | memórias vivas (inclui `shared`) |
| `memory_save` | `tenant, scope, kind, content, lifecycle="important", domain?, project?, importance=0.5` | memória curta e durável |
| `ingest_note` | `tenant, title, content, domain?, source_uri?` | documento markdown (relatórios, ADRs, notas longas) |

Domínios válidos: `chief`, `engineering`, `finance`, `projects`, `personal`, `learning`.

## Memória (`memory_save`)
- **scope:** `global` (vale para tudo no tenant) · `domain` (exige `domain`) · `project` (exige `project`
  = slug **cadastrado**; slug desconhecido gera erro "unknown project" → use `scope="domain"` e comece
  o texto com `[projeto <slug>]`).
- **kind:** `preference`, `working_style`, `communication`, `rule`, `constraint`, `fact`, `decision`,
  `architecture`, `roadmap` (inclui metas), `issue`, `episode` (o que aconteceu num dia/sessão), `procedure`.
- **lifecycle:** `temporary` (expira em 14 dias — planos do dia, lembretes), `important` (padrão),
  `persistent` (regras, decisões vigentes, metas). `deprecated` só via manutenção.
- **importance:** 0–1 (padrão 0.5); decisões e regras vigentes 0.7–0.9; episódios 0.3–0.5.
- **Substituição automática:** memória nova com similaridade ≥ 0.92 a uma existente do mesmo
  tenant/scope/domain/project/kind a substitui (o histórico fica). Resultado `action`:
  `created` | `superseded` | `duplicate`.
- **Limite:** 4000 caracteres; acima disso use `ingest_note`.

## Busca
- `memory_search` ordena por similaridade semântica, não por data, e só devolve memórias vivas (uma
  substituída some). Para "o que aconteceu no dia X": filtre `domain`+`kind` com `k` alto e leia as datas
  no texto, ou busque notas datadas com `knowledge_search` (a busca híbrida casa a data literal
  `AAAA-MM-DD` no título/conteúdo).
- Projeto: não use o filtro `project` para buscar — memórias de projeto não cadastrado estão em
  `scope="domain"`. Use `query="[projeto <slug>] <tema>"`, que acha os dois casos.

## Documentos (`ingest_note`)
- `source_uri` define a identidade do documento no tenant: mesmo URI = atualização; sem `source_uri`
  o URI vem do hash de título + conteúdo, e cada versão vira um documento vivo novo.
- Esquemas aceitos: `agent://`, `storage://`, `obsidian://`, `manual://`, `https://`, `http://`
  (`file:///` não). Convenções: `agent://notes/<domínio>/<slug>` (nota que evolui),
  `agent://learning/<AAAA-MM-DD>/<slug>` (registro de sessão de estudo, nunca reescrito),
  `agent://research/<slug>`, `agent://research/sintese/<slug>`, `agent://adr/<projeto|geral>/<id>`.

## O que vira o quê
| Situação | Ferramenta | Exemplo |
|---|---|---|
| Fato curto que muda decisões futuras | `memory_save` | "Usuário prefere respostas curtas com o resultado primeiro" (`shared`, `global`, `communication`, `persistent`) |
| Decisão tomada | `memory_save` + `ingest_note` (ADR) | skill `decision_record` |
| Resumo, relatório, pesquisa, aula | `ingest_note` | "Síntese: filas em Go (2026-10-07)" |
| Plano/registro do dia | `memory_save` `episode` `temporary` | "2026-10-07: plano — 1) … 2) …" |
| Sessão de estudo | `ingest_note` (URI por data+tema) + `memory_save` `episode` `important` | skill `knowledge_capture`, passo 6 |
| Conversa trivial, dado já na base, segredo, dado bruto (extrato, log) | nada | — |

## Boas memórias
- Atômicas (um fato), autocontidas (entendíveis sem a conversa), datadas quando o tempo importa,
  na terceira pessoa ("O usuário…"), sem segredos nem dados sensíveis desnecessários.
- Antes de salvar: `memory_search` com o mesmo tema; se já existe, só salve se for correção/atualização.

## Projetos cadastrados

`scope="project"` só funciona para slugs cadastrados no knowledge: o usuário cadastra com
`make kb-project tenant=<t> slug=<slug> name="<nome>" [repo=<git-url>]`, e o agente pode cadastrar com
`mcp__knowledge__project_upsert(tenant, slug, name, domain?, repository?)` quando o usuário nomeia um projeto
novo. `mcp__knowledge__project_list(tenant)` lista os projetos (com `repository`). Enquanto o slug não existe,
continua valendo o fallback: `scope="domain"` com o prefixo `[projeto <slug>]` no texto.
