# skills — Hermes skills do AI Agent OS

Capacidades sob demanda (ARCHITECTURE §5, CONTRACTS §3): cada skill é um `SKILL.md` no formato do
Hermes Agent v2026.9.24, com scripts determinísticos onde a tarefa permite. O Hermes monta este
diretório read-only em `/opt/shared-skills` (`skills.external_dirs`); os prompts das revisões
agendadas ficam em [`../workflows/`](../workflows/).

```
skills/<categoria>/DESCRIPTION.md            descrição da categoria (índice de skills do Hermes)
skills/<categoria>/<nome>/SKILL.md           frontmatter Hermes + instruções (When to Use, Procedure, ...)
skills/<categoria>/<nome>/scripts/           scripts stdlib Python 3.11+ / bash, rodados no sandbox
skills/<categoria>/<nome>/{references,templates}/   material carregado sob demanda (skill_view)
skills/tests/                                pytest + validador que usa o próprio código do Hermes
```

## Catálogo

| Categoria | Skill | Para quê | Script |
|---|---|---|---|
| coding | `repository_analysis` | mapa do repositório: stack, comandos, entrypoints, riscos | `scripts/repo_map.sh` |
| coding | `debugging` | reproduzir → isolar → corrigir → validar (gate `aios`) | — |
| coding | `code_review` | revisão de diff/PR por severidade | `references/checklist.md` |
| coding | `architecture_review` | opções e trade-offs com evidência; termina em ADR | — |
| coding | `test_generation` | teste que falha antes e passa depois | — |
| finance | `spreadsheet_analysis` | perfil CSV/XLSX: colunas, totais, categoria, mês, variação | `scripts/analyze.py` |
| finance | `budget_review` | orçado × realizado por categoria | `scripts/budget_check.py` |
| finance | `expense_categorization` | regras palavra-chave → categoria; LLM só nas sobras | `scripts/categorize.py` |
| research | `web_research` | pesquisa com fontes primárias e citações | — |
| research | `source_synthesis` | conclusões com grau de confiança a partir de várias fontes | `templates/evidence_matrix.md` |
| projects | `project_status` | status com evidência (Kanban, memória, repo) | — |
| projects | `roadmap_update` | diff de roadmap proposto e gravado só com aprovação | — |
| projects | `decision_record` | ADR + memória `decision` | `templates/adr.md` |
| productivity | `daily_planning` | até 3 prioridades, blocos de tempo, delegação | — |
| productivity | `daily_review` | revisão manhã/noite: plano × realizado | — |
| productivity | `weekly_review` | reinício semanal com 3–5 resultados | — |
| knowledge | `knowledge_capture` | o que vira memória, nota ou nada (por tenant) | `references/memory-model.md` |
| knowledge | `memory_hygiene` | duplicadas, conflitantes, vencidas | — |
| knowledge | `transcript_to_notes` | mídia → edge (whisper + Qwen local) → notas e tarefas | `scripts/edge_pipeline.sh` |

`config/routing.yaml` (`task_types[*].skills`) e `config/hermes/profiles.yaml` (`cron[*].skills`)
referenciam estas skills como `<categoria>/<nome>`; os testes garantem que todas existem.

## Convenções de todas as skills

- **Determinístico primeiro**: scripts, testes (`aios-check --json <path>`), SQL/duckdb antes de raciocinar.
- **Tenant explícito** em toda ferramenta `mcp__knowledge__*` e `kanban_create`: `nitro` (trabalho),
  `pessoal` (vida pessoal), `shared` (preferências globais). Uma sessão = um tenant: o plugin `aios`
  bloqueia outros tenants (exceto `shared`).
- **Gate em vez de troca de modelo**: toda skill tem `## Gate e escalonamento`. Só edição de código
  passa pelo gate automático (hook `pre_verify` → `aios-check` → Decision Service `repair` / `escalate` /
  `done` / `fail` / `ask_human`). Para revisão, pesquisa, finanças, projetos e produtividade não há
  validador: a skill diz qual evidência mostrar e, com evidência fraca em algo crítico, manda parar e
  perguntar (ou `kanban_block`). Nenhuma skill pede outro modelo; "orçamento esgotado" = parar e relatar.
- **Delegação** por Kanban: `kanban_create(assignee=engineering|finance|projects|personal|learning,
  tenant=..., skills=[...], idempotency_key=...)`.
- **Memória só do que é durável** (`memory_save`), com scope/kind/lifecycle de
  `knowledge/knowledge_capture/references/memory-model.md`.
- **Projetos**: `scope="project"` só funciona com o slug cadastrado (`make kb-project`,
  `mcp__knowledge__project_upsert`; `project_list` lista com o `repository`). Toda skill que grava por
  projeto põe `[projeto <slug>]` no início do texto e, no erro "unknown project", repete com
  `scope="domain"`; as buscas não usam o filtro `project` (que esconderia as memórias do fallback) e sim
  `query="[projeto <slug>] …"`.
- **Documentos com `source_uri` estável** (`agent://notes|research|learning|adr/...`): mesmo URI atualiza
  o documento; sem ele cada versão vira outro documento vivo. Os testes conferem que todo `source_uri`
  citado usa um esquema aceito por `kb.ingest.ALLOWED_URI_SCHEMES`.
- Formato Hermes: `name` = diretório (`[a-z0-9_-]`), `description` ≤ 60 caracteres terminando em ponto,
  `version`/`author`/`license`, `metadata.hermes.{category,tags,related_skills,requires_toolsets}`,
  seção `## When to Use`; nenhuma ferramenta de shell citada em prosa (o linter do Hermes reclama).

## Scripts no sandbox

O `terminal` do Hermes roda no container `sandbox` via SSH. O backend SSH do Hermes sincroniza os
diretórios de skills para `~/.hermes/external_skills/<i>/` no sandbox; por isso cada SKILL.md usa:

```bash
D="${HERMES_SKILL_DIR}"; [ -d "$D" ] || D=$(ls -d ~/.hermes/external_skills/*/<categoria>/<nome> 2>/dev/null | head -n 1)
```

(`${HERMES_SKILL_DIR}` é o caminho no container do Hermes, `/opt/shared-skills/...`.)

| Script | Uso | Saída |
|---|---|---|
| `finance/spreadsheet_analysis/scripts/analyze.py` | `analyze.py <csv/xlsx> [--sheet] [--skip-rows N] [--amount-col ...] [--export-csv out.csv]` | JSON: colunas, totais, `by_category`, `by_month`, `mom`, `by_category_month`, `top`, `warnings` |
| `finance/budget_review/scripts/budget_check.py` | `analyze.py ... \| budget_check.py - --budget orcamento.yaml [--month AAAA-MM]` | JSON: status `over`/`warn`/`ok` por categoria |
| `finance/expense_categorization/scripts/categorize.py` | `categorize.py <csv/json> --rules regras.yaml [--decimal comma\|dot] [--output-csv]` | JSON: cobertura, `by_category`, `unknown_groups` |
| `coding/repository_analysis/scripts/repo_map.sh` | `repo_map.sh [--format json\|md] [--commits N] <repo>` | JSON/markdown do repositório (UTF-8 sempre válido) |
| `knowledge/transcript_to_notes/scripts/edge_pipeline.sh` | `edge_pipeline.sh [--domain D] <storage://...> <tenant>` ou `--upload <arquivo> <tenant>` | JSON do resultado do edge; exit 7 se `<tenant>` ≠ `HERMES_TENANT` |

`analyze.py` usa o CLI `duckdb` quando existe (lê CSV/XLSX como texto, extensão `excel` autoload)
e cai para `csv` + leitor XLSX stdlib; tipagem e somas são sempre em Python (`Decimal`), então os dois
motores dão o mesmo resultado — inclusive em extratos com preâmbulo ("Extrato…", "Agência…", linha em
branco): o duckdb pula pelo sniffer, o leitor Python escolhe a primeira linha com cara de cabeçalho na
largura usual da tabela (`--skip-rows N` força). Formatos BR (`1.234,56`, `R$`, `(10,00)`, `dd/mm/aaaa`,
CP-1252, `;`) são detectados; uma coluna "Tipo" (PIX/TED) não é tomada como categoria. `categorize.py`
decide o separador decimal uma vez por coluna, com as mesmas regras do `analyze.py` (totais iguais nos
dois), e usa PyYAML se instalado; senão um parser do subconjunto documentado. `budget_check.py` lê
limites no padrão BR (`1.500` = mil e quinhentos; `1,500` é recusado por ambíguo).

## Revisões agendadas (`workflows/*.md`)

Cada arquivo é o prompt inteiro de um job do cron do Hermes no chief (criado por `make hermes-setup`
→ `infra/hermes/setup.py` a partir de `config/hermes/profiles.yaml`; o arquivo é lido sem frontmatter).
Cada execução é **de um tenant só**, declarado na primeira linha.

| Job | Quando | Tenant | Skill seguida |
|---|---|---|---|
| `morning-review` | 07:30 diário | pessoal | `daily_review` (modo manhã) |
| `daily-planning` | 08:00 seg–sex | nitro | `daily_planning` |
| `engineering-review` | 09:00 seg–sex | nitro | `project_status` |
| `project-review` | 18:30 seg–sex | nitro | `project_status`, `roadmap_update` |
| `learning-review` | 20:00 diário | pessoal | `knowledge_capture` |
| `daily-reflection` | 22:30 diário | pessoal | `daily_review` (modo noite) |
| `weekly-review` | dom 19:00 | pessoal | `weekly_review` |
| `weekly-review-nitro` | sex 17:30 | nitro | `weekly_review` |

Como os prompts são escritos:
- Os jobs não anexam skills (`config/hermes/profiles.yaml`): cada workflow carrega a sua com `skill_view`,
  e a linha `Tenant desta execução` fica em ~1.200 caracteres, dentro da janela que o plugin `aios` roteia
  (o plugin também lê essa tag em qualquer posição da mensagem).
- Orçamento: uma rodada paralela com todas as leituras, uma com as gravações (`kanban_create`,
  `memory_save`) e só então a entrega; "orçamento esgotado" = entregar o que tiver.
- Histórico por data: `memory_search` ordena por similaridade, então os workflows filtram
  `domain`+`kind` com `k` alto e leem as datas no texto; a revisão espaçada busca notas
  `agent://learning/<data>/<tema>` por data literal (`knowledge_search` é híbrida).
- Delegação a workers leva no `body` o que eles não enxergam: a lista de tarefas do projeto (workers não
  têm `kanban_list`) e `repo: <git-url> ref: <branch>` para tarefas de código (sem URL, o repo vai para
  "Precisa de você").

Cobertura: lacunas aceitas — não há
planejamento diário pessoal (a revisão da manhã sugere o foco do dia) nem revisão da manhã do trabalho
(o `daily-planning` das 08:00 cobre as pendências do último dia útil). Para outro job, copie um
workflow, troque o tenant da primeira linha e de todas as chamadas, e registre-o.

## Validação e testes

```bash
make skills-test                                    # pytest: formato, referências, workflows, scripts em fixtures
HERMES_PYTHON=<hermes>/.venv/bin/python make skills-test   # + validação com o código do próprio Hermes
make skills-validate                                # o mesmo validador dentro do container hermes
```

Com `HERMES_PYTHON`, `skills/tests/hermes_cron_prompt.py` monta o prompt de cada job com o
`_build_job_prompt` do próprio Hermes e mede onde cai a linha do tenant (com e sem skills anexadas).
Os testes também conferem o toolset `kanban` do chief e a posição da linha do tenant nos jobs configurados.

`skills/tests/hermes_validate.py` cria um `HERMES_HOME` descartável com `skills.external_dirs` apontando
para cá e roda: `_validate_frontmatter(new_skill=True)`, o linter `tools/skill_linter.py` (zero
findings), o scanner `tools/skills_guard.py` (veredito `safe`), `skills_list`/`skill_view` (nome simples
e `categoria/nome`, sem warnings), o índice de skills do system prompt e os scanners de prompt do
cron (`workflows/*.md` no modo estrito). Equivalente manual:
`HERMES_HOME=/tmp/x hermes skills list --source local` com `skills.external_dirs` no `config.yaml`.

## Make targets (`mk/skills.mk`)

| Target | O quê |
|---|---|
| `skills-test` | pytest de `skills/tests` (com `HERMES_PYTHON`, inclui o validador do Hermes); também roda em `make test` |
| `skills-validate` | validador do Hermes dentro do container `hermes` |
| `skills-memory-deprecate tenant=… ids="…"` | descontinua memórias aprovadas na skill `memory_hygiene` (`POST /v1/memories/{id}/deprecate`) |

## ENV

| Variável | Segredo? | Padrão | Uso |
|---|---|---|---|
| `EDGE_API_KEY` | sim | — (no worker: a chave do tenant da tarefa, de `EDGE_TENANT_KEYS`, posta pelo plugin `aios`) | `edge_pipeline.sh`; declarada em `required_environment_variables` de `transcript_to_notes`, o Hermes a repassa ao sandbox via SSH `SendEnv` |
| `AIOS_EDGE_URL` | não | `http://edge:8080` | `edge_pipeline.sh` |
| `AIOS_EDGE_TIMEOUT` | não | `1800` | `edge_pipeline.sh`: segundos de espera pelo job |
| `HERMES_TENANT` | não | — (o Hermes define em workers Kanban) | declarada `optional` em `transcript_to_notes`; `edge_pipeline.sh` recusa tenant diferente (exit 7) |
| `HERMES_PYTHON` | não | vazio | só testes: python do venv do Hermes para o teste de validação |

Variável de make: `KNOWLEDGE_URL` (padrão `http://127.0.0.1:8092`) em `skills-memory-deprecate`.

## Integração com os outros componentes

Resolvido fora deste diretório (os testes daqui conferem o que dá):

- **Tenant do cron**: jobs sem skills anexadas + o plugin `aios` honra a tag `Tenant desta execução` em
  qualquer posição e roteia sobre a instrução sem o corpo das skills.
- **Orçamento**: o Decision Service conta só tokens novos (entrada não cacheada + saída), e os
  `token_budget.total` dos agentes foram dimensionados para jobs de revisão (120k–400k).
- **Kanban no chief**: `platform_toolsets` do chief inclui `kanban`; os perfis de domínio não.
- **Projetos**: `project_list`/`project_upsert` (MCP), `/v1/projects` e `make kb-project`.
- **Sandbox/edge**: `AcceptEnv GITHUB_TOKEN EDGE_API_KEY HERMES_TENANT`, edge na `sandbox_net`, e chave do
  edge por tenant (`EDGE_TENANT_KEYS`): um worker só recebe a chave do tenant da sua tarefa, e o edge recusa
  qualquer outro tenant. Fora de um worker do Kanban não há chave de edge; delegue ao `engineering`.
