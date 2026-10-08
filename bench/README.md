# bench — bateria de testes da V1

Implementa `docs/CONTRACTS.md` §7 e `docs/ARCHITECTURE.md` §24.18 / §14. São tarefas reais, no contexto do Leo:
Go/TypeScript/Python, o mini-ERP, os apps da Nitro, planilhas de finanças pessoais e estudos de inglês/tecnologia.

Cada tarefa é enviada ao **Hermes**, que é o único runtime de agentes. A tarefa então é verificada de forma
determinística ou por um juiz LLM. O custo, os tokens e o tier vêm do **Decision Service**. O resultado volta
para o ledger, para o roteamento aprender com ele, e fica gravado em `bench_runs`.

O bench não é um agente: não tem loop, não toma decisões e não orquestra nada.

```
bench run ─┬─ stage do fixture ──ssh agent@sandbox (tar)──▶ /workspace/bench/<session_id>
           ├─ decision POST /v1/route {session_id, text, tenant, agent}       (tenant/agente reais da tarefa)
           ├─ hermes  POST [/p/<perfil>]/v1/runs {input, session_id, instructions, model?} + Idempotency-Key
           │          GET  …/v1/runs/{run_id}/events (SSE: tool.started, approval.request → deny, run.*)
           │          GET  …/v1/runs/{run_id}            (status/output/usage/runtime; fallback por polling)
           ├─ check   command (no sandbox) | regex | json_schema (na resposta final) | llm_judge (LiteLLM)
           ├─ decision GET /v1/runs/{session_id}          (modelo inicial/final, tier, tokens, custo, iterações…;
           │                                               relido até o último /v1/usage do plugin chegar)
           ├─ decision POST /v1/runs/{session_id}/finish {succeeded|failed|cancelled, task_type: esperado}
           └─ INSERT bench_runs                           (migrations/013_bench.sql)
```

## A bateria

| Categoria | Tarefas | Como é verificada |
|---|---|---|
| simple | 10 | regex/JSON na resposta (commit em inglês, regex de CPF, SQL, cron, extração, juros…), 1 juiz |
| medium | 10 | repositórios pequenos com testes (CPF em Go, slug em TS, tabela Price, SQL, API net/http, Dockerfile), code review em JSON, log 5xx, plano de estudos, status do mini-ERP |
| debugging | 10 | repositórios com teste falhando (Go ×4, Python ×3, TS ×2) + stack trace de panic |
| refactor | 5 | testes continuam passando **e** o problema de código sumiu (tamanho da função, duplicação, callbacks, mapa global, números mágicos) |
| agentic | 5 | CLI Go do zero, `git bisect`, organizar fotos + CSV, script de MAU, deixar o CI verde |
| research | 5 | release notes do Go (regex) + 4 com juiz LLM e rubrica de fatos verificados (Pix Automático, SQLite×Postgres, Whisper local, IELTS×TOEFL) |
| finance | 5 | CSVs com respostas conhecidas: números checados por regex com normalização pt-BR, ou por JSON schema |
| media | 5 | 3 resumos de áudio/vídeo de verdade (fala via espeak-ng, vídeo via ffmpeg, transcritos pelo edge), uma ata a partir de transcrição `.srt` e a inspeção de um WAV; checa palavras-chave e itens de ação |

**Total: 55 tarefas.** As contagens por categoria (10/10/10/5/5/5/5/5) vêm de ARCHITECTURE §24.18 e são o que
`bench validate` exige. CONTRACTS §7 fala em "60", mas a soma dessas contagens dá 55. Se a intenção for ter 60,
ajuste `bench.CATEGORIES` e acrescente as 5 tarefas.

**Desvio consciente em media:** §24.18 pede "5 tarefas de resumo de áudio/vídeo". Só 3 delas passam por áudio ou
vídeo de verdade (`media-recado-whatsapp`, `media-aula-concorrencia`, `media-video-aula-ingles`) e dependem de
espeak-ng/ffmpeg e do edge. `media-ata-reuniao` resume uma transcrição `.srt` já pronta (a etapa depois do
Whisper) e `media-metadados-audio` confere o formato de um WAV antes de transcrever. Essas duas rodam sem edge,
então a categoria continua medida mesmo onde o pipeline de mídia não está disponível.

### Formato da tarefa (`tasks/<categoria>/<key>.yaml`)

O schema completo está em `src/bench/task.schema.json` (JSON Schema 2020-12).

```yaml
key: debug-go-desconto-centavos      # = nome do arquivo; vai no session_id
category: debugging
title: Total com desconto 1 centavo acima do esperado (Go)
tenant: nitro                        # nitro | pessoal | shared  (vai para /v1/route)
agent: engineering                   # perfil Hermes (opcional; sem ele = chief)
fixture: debug-go-desconto-centavos  # bench/fixtures/<fixture>/, copiado para o workdir
setup: bash criar-repo.sh            # opcional, roda no workdir depois do stage
generated_files: [aula.wav]          # opcional, arquivos que `bench fixtures` precisa ter gerado
requires: [edge]                     # opcional, serviços que o agente precisa alcançar (sondados antes do run)
timeout_s: 1200                      # opcional (padrão BENCH_TASK_TIMEOUT)
expected_task_type: debugging        # opcional, mede o roteador e é enviado no /finish
expected_domain: engineering         # opcional, mede o roteador
prompt: |                            # pt-BR; {workdir} vira o caminho do fixture no sandbox
  … O módulo Go está em {workdir} …
check:
  type: command                      # roda no workdir, via ssh no sandbox (ou local com --local)
  run: go vet ./... && go test -count=1 ./...
  expect_exit: 0
  protect: [desconto_test.go]        # sha256 conferido antes do comando: o agente não pode "consertar" o teste
```

Os outros tipos de check:

- `{type: regex, pattern, normalize_numbers?}`: o padrão é aplicado à resposta final. Com `normalize_numbers`,
  `R$ 1.234,56` vira `1234.56` antes do match.
- `{type: json_schema, schema}`: passa se algum JSON da resposta valida no schema. A busca olha bloco cercado,
  depois a resposta inteira, depois o primeiro `{…}`/`[…]`.
- `{type: llm_judge, rubric, pass_score}`: nota de 0 a 10 dada por `BENCH_JUDGE_MODEL` via LiteLLM
  (`BENCH_LITELLM_KEY`), com temperatura 0.

Regras que `bench validate` aplica além do schema:

- **Arquivos e contagens:**
  - chave única, igual ao nome do arquivo;
  - categoria igual ao diretório;
  - contagens exatas por categoria.
- **Fixture e `{workdir}`:**
  - o diretório do fixture existe;
  - `{workdir}` aparece no prompt se, e somente se, houver fixture;
  - check `command` exige fixture;
  - os caminhos em `protect` existem;
  - todo `generated_files` é produzido por `bench fixtures`.
- **Checks:**
  - a regex compila;
  - o schema do `json_schema` é válido.
- **Roteamento e agentes:**
  - `expected_task_type` existe em `config/routing.yaml`;
  - o agente existe e pode acessar o tenant da tarefa;
  - check `command` exige um perfil com shell.
  - Tarefa com fixture exige um perfil com shell ou filesystem. Delegar via Kanban é assíncrono, então o run
    terminaria antes de dar para verificar.

### Fixtures

Os fixtures de código são repositórios mínimos de verdade: módulos Go sem dependências, Python só com stdlib e
`unittest`, TypeScript rodando com `node --test` (type stripping, Node ≥ 22.18).

`tests/test_fixtures.py` garante duas coisas para cada tarefa `command`:

1. o fixture intacto **falha** no check;
2. a solução de referência em `tests/solutions/<key>/` **passa**, com os arquivos protegidos intactos.

As soluções nunca vão para o sandbox.

## Uso

```bash
make bench-validate                              # lint + contagens
make bench-test                                  # pytest (offline)
make bench-fixtures                              # gera tom.wav sempre; fala/vídeo se houver espeak-ng/ffmpeg
make bench-dry-run category=debugging repeat=2   # plano, sem chamar nada
make bench-run category=debugging repeat=3       # no container (rede aios: hermes, decision, sandbox, postgres)
make bench-run task="simple-juros-compostos finance-cdb-vs-lci" repeat=3   # só essas tarefas
make bench-report since=7d                       # tabelas + diff sugerido do routing.yaml
```

Direto pela CLI:

```
bench run [--category C]... [--task KEY]... [--repeat N] [--model ALIAS] [--dry-run] [--local] [--keep-workdir] [--no-db]
bench report [--since 7d|24h|2w|2026-10-01] [--min-runs 3] [--target 0.8] [--json]
bench validate | bench list [--category C] | bench fixtures [--force]
```

- `session_id = bench-<key>-<n>-<AAAAMMDDhhmmss UTC>`. É o mesmo id na sessão Hermes, na run do ledger e em
  `bench_runs`.
- `--local` copia os fixtures para `BENCH_WORKSPACE` (padrão `$TMPDIR/aios-bench`) e roda os checks nesta
  máquina. Só serve quando o Hermes usa o terminal local.
- **Saída:** 0 quando a bateria rodou (falhas são dados). 1 se houve erro de infraestrutura ou se alguma linha
  não foi gravada. 2 para erro de uso.
- **Pedidos de aprovação** (`approval.request`) são sempre negados: o bench nunca aprova ação perigosa.
- **Erros de infraestrutura** viram linhas com `error`. Essas linhas vão ao ledger como `cancelled`, então não
  ensinam uma falha ao roteador, e ficam fora das taxas e dos custos do relatório. Contam como erro:
  - Hermes recusou o run, ssh caiu, juiz indisponível;
  - o run terminou `interrupted` (gateway reiniciou) ou `cancelled` sem ser o bench a parar;
  - o run terminou `failed` **com** `error`: provedor fora do ar, 401, LiteLLM devolvendo 400 "Budget has been
    exceeded" para a `HERMES_LITELLM_KEY`, exceção no runtime. Sem isso, uma queda no meio da bateria viraria
    falha de modelo em todas as tarefas seguintes.

  `failed` **sem** `error` (orçamento de iterações, turno parcial) continua sendo falha do agente.
- **Timeout:** o bench pede `/stop` e espera até 60 s o run chegar a um status terminal. A tarefa conta como
  falha (`check_detail.reason = "timeout"`) e o check **não** é avaliado, porque a árvore pode estar pela metade.
  Se o run ainda não terminou depois do `/stop`, o workdir é mantido (`check_detail.workdir_kept`), para não
  apagar o diretório onde o agente ainda roda comandos.
- **Pré-condições (`requires`):** antes da primeira tarefa que declara `requires: [edge]`, o bench roda uma
  sonda onde o agente trabalha (ssh no sandbox ou local com `--local`). A sonda manda `EDGE_API_KEY` com um valor
  fictício via `SendEnv`, do mesmo jeito que o terminal do Hermes manda a chave real, e confere que ela chegou e
  que `$AIOS_EDGE_URL/healthz` responde. Se falhar, as tarefas são puladas (`SKIP … requires edge: …`) em vez de
  virarem falha de modelo. O bench nunca guarda a chave real.
- **Sem ledger:** se o Decision Service não tem uso para a sessão, os tokens vêm do `usage` do Hermes e o custo
  é calculado com os preços do `routing.yaml`, pela mesma fórmula do `Policy.Cost`. A linha fica marcada com
  `check_detail.usage_source = "hermes"`.

### Isolamento das sessões de teste

Os prompts estão em primeira pessoa e trazem dados fictícios (renda, colegas, reuniões), e as sessões rodam nos
tenants reais. Para que nada disso vire memória ou trabalho assíncrono, as `instructions` de todo run (`INSTRUCTIONS`
em `src/bench/hermes.py`) proíbem salvar memórias (`memory`, `memory_save`, `ingest_note`), criar tarefas Kanban
ou cron e enviar mensagens, e mandam resolver tudo na mesma sessão. A barreira de verdade é o plugin `aios`: no
`pre_tool_call`, ele bloqueia essas ferramentas (`memory`, `memory_save`, `ingest_note`, `kanban_create/link/comment`,
`cronjob_manage`, `send_message`) em toda sessão cujo `session_id` começa com `bench-`.

### Perfis (tarefas com `agent`)

O Hermes atende perfis nomeados em `/p/<perfil>/v1/...`, e cada perfil exige **sua própria** `API_SERVER_KEY`.
Sem `HERMES_API_KEY_<PERFIL>`, a tarefa roda no perfil padrão (chief), que não tem shell. Tarefas de código vão
falhar, e o `bench run` avisa no início. Para habilitar um perfil:

```bash
k=$(openssl rand -hex 32)
echo "API_SERVER_KEY=$k" >> data/hermes/profiles/engineering/.env   # depois reinicie o gateway
echo "HERMES_API_KEY_ENGINEERING=$k" >> .env
```

### Mídia

O tom (`tom.wav`, 12 s, 16 kHz, mono) é gerado só com a stdlib. Fala e vídeo precisam de `espeak-ng`, e o vídeo
também de `ffmpeg`. Ambos são opcionais: sem eles, `bench run` pula as 3 tarefas que dependem desses arquivos
(`SKIP … missing generated files`).

As tarefas de fala usam a skill `knowledge/transcript_to_notes` (`edge_pipeline.sh --upload --no-ingest`). O
`--no-ingest` impede que áudio de teste entre na base de conhecimento. O script precisa de `EDGE_API_KEY` no
terminal do sandbox, e o `sshd` do sandbox hoje só aceita `GITHUB_TOKEN` (`AcceptEnv`). Enquanto isso não mudar
em `workers/sandbox`, a sonda `requires: [edge]` pula essas 3 tarefas.

## Relatório (§14)

- **Agrupamento:** `bench report` agrupa por categoria × `start_model` (o modelo que o roteamento escolheu,
  lido do ledger) e por tipo de tarefa × `start_model`.
- **Métricas por grupo:** runs, erros, taxa de sucesso, **custo por tarefa concluída**, custo total, média de
  tokens e de iterações, taxa de escalonamento e acurácia do roteador (tipo e domínio).
- **Acurácia de domínio:** só é medida quando o pre-route vai como `agent=chief`. Com um agente nomeado
  (`/p/engineering`…), o Decision Service copia o domínio do agente sem perguntar ao classificador. Nesse caso
  `router_domain_ok` fica nulo e `check_detail.domain_pinned_by_agent = true`. Sem isso, a métrica dependeria de
  quais `HERMES_API_KEY_<PERFIL>` estão configuradas, e não da qualidade do roteador.
- **Tokens e custo:** o plugin manda `/v1/usage` em segundo plano depois de cada chamada. O bench relê
  `GET /v1/runs/{session_id}` (até 5 vezes, 1 s entre leituras) até os input tokens do ledger alcançarem os do
  Hermes ou `llm_calls` parar de mudar (`check_detail.ledger_settle_reads`).
- **Custo por sucesso** = custo de todas as tentativas ÷ número de sucessos. É o exemplo do §14: K2.7 a
  US$ 0,25 × 4 tentativas = US$ 1,00 por sucesso, contra Sonnet a US$ 0,80 × 1 = US$ 0,80, então o Sonnet ganha.
- **Sugestão por tipo de tarefa:** o modelo de menor custo por sucesso entre os que têm sucesso ≥
  `learning.target_success_rate` (0,8) e pelo menos `--min-runs` runs.
- **Diff unificado do `config/routing.yaml`:** só troca `tier_by_complexity.<nível>` dos níveis de
  complexidade em que esse modelo foi de fato medido. Preserva os comentários, é revalidado com YAML, vai para
  o stdout e **nunca é aplicado**.

A view `v_bench_summary` tem as mesmas métricas por categoria × modelo, para consultar no psql.

## Tabela `bench_runs` (migrations/013_bench.sql)

Colunas pedidas:

- `run_at`, `task_key`, `category`, `session_id`;
- `start_model`, `final_model`, `tier`, `success`, `check_detail` (jsonb);
- `input_tokens`/`output_tokens`, `cost_usd`;
- `iterations`, `repairs`, `escalations`, `duration_ms`;
- `router_task_type_ok`, `router_domain_ok`.

Colunas extras:

- identificação da execução: `batch_id`, `repeat_index`;
- do lado do Hermes: `hermes_run_id`, `hermes_status`, `hermes_profile`;
- da tarefa: `tenant`, `agent`, `requested_model`;
- do roteamento: `task_type`/`expected_task_type`, `domain`/`expected_domain`, `complexity`;
- de verificação e consumo: `check_type`, `cache_read_tokens`, `tool_calls` (contados pelo stream de eventos,
  §24.18 "tool calls");
- `error`.

`aios_reader` tem `SELECT`.

## ENV

| Variável | Segredo? | Padrão | Uso |
|---|---|---|---|
| `AIOS_HERMES_URL` | não | `http://127.0.0.1:8642` (container: `http://hermes:8642`) | API do Hermes |
| `HERMES_API_KEY` | **sim** | — (existente) | perfil padrão (chief) |
| `HERMES_API_KEY_ENGINEERING`, `_FINANCE`, `_PROJECTS`, `_PERSONAL`, `_LEARNING` | **sim** | vazio | `/p/<perfil>/v1/runs`; igual ao `API_SERVER_KEY` do `.env` do perfil |
| `AIOS_DECISION_URL` | não | `http://127.0.0.1:8090` (container: `http://decision:8080`) | ledger, route, finish |
| `DECISION_API_KEY` | **sim** | — (existente) | |
| `BENCH_DATABASE_URL` | **sim** (tem senha) | `postgresql://aios:$AIOS_DB_PASSWORD@127.0.0.1:$PG_HOST_PORT/aios` | `bench_runs` |
| `LITELLM_BASE_URL` | não | `http://127.0.0.1:4000` | juiz |
| `BENCH_LITELLM_KEY` | **sim** | vazio (criado por `infra/scripts/litellm-keys.sh`) | juiz LLM |
| `BENCH_JUDGE_MODEL` | não | `tier5-sonnet` | modelo do juiz |
| `BENCH_TASK_TIMEOUT` | não | `900` | segundos por tarefa (sobrescrito por `timeout_s`) |
| `BENCH_POLL_INTERVAL` | não | `2` | polling do status do run |
| `BENCH_HTTP_TIMEOUT` | não | `30` | timeout das chamadas HTTP |
| `BENCH_WORKSPACE` | não | `/workspace/bench` (sandbox); `$TMPDIR/aios-bench` com `--local` | onde os fixtures são copiados |
| `TERMINAL_SSH_HOST`/`_USER`/`_PORT`/`_KEY` | não (a chave é um arquivo) | — / `agent` / `22` / — | mesmo alvo do terminal do Hermes |
| `BENCH_SSH_KNOWN_HOSTS` | não | padrão do ssh | `UserKnownHostsFile` |
| `AIOS_ROUTING_FILE` | não | `<repo>/config/routing.yaml` | preços, tipos de tarefa, patch |
| `AIOS_AGENTS_DIR` | não | `<repo>/agents` | `bench validate` |
| `BENCH_HOME`, `BENCH_TASKS_DIR`, `BENCH_FIXTURES_DIR` | não | `bench/`, `bench/tasks`, `bench/fixtures` | |
| `BENCH_ENV_FILE` | não | `.env` do repo, se existir | variáveis já definidas no ambiente nunca são sobrescritas |
| `BENCH_LOG_FORMAT` | não | `json` | logs no stderr (`json` ou `text`) |
| `BENCH_TEST_DATABASE_URL` | **sim** | vazio | só para os testes: ativa o teste de integração com Postgres |

## Testes

```bash
cd /home/dev/agent-system && ~/.local/bin/uv run --project bench pytest -q bench/tests
BENCH_TEST_DATABASE_URL=postgresql://aios:…@127.0.0.1:5432/aios ~/.local/bin/uv run --project bench pytest -q bench/tests
```

Os testes cobrem:

- **Tarefas:** lint das tarefas e contagens.
- **Checks:**
  - cada tipo de check;
  - normalização de números e extração de JSON;
  - respostas de ouro (`tests/golden_answers.yaml`): cada tarefa regex/JSON tem respostas que passam e
    respostas que falham.
- **Fixtures:**
  - fixture intacto falha e solução de referência passa (precisa de go/node/python3/git; sem eles, pula);
  - setup do `git bisect`.
- **Relatório:** a conta do §14, o patch do `routing.yaml` (cópia congelada e arquivo vivo) e o `build_report`.
- **Runner:** contra um Hermes e um Decision Service *stub* (`tests/stubs.py`, mesmo formato do wire):
  - perfil `/p/engineering`, Idempotency-Key, pre-route, `/finish` com o tipo esperado, domínio fixado pelo agente;
  - 429 com retry, erro 400, aprovação negada, SSE indisponível;
  - run `failed` com `error`/`interrupted`/`cancelled` → `cancelled` no ledger; `failed` sem `error` → falha;
  - timeout com `/stop`: sem avaliação do check, workdir mantido enquanto o run não termina;
  - releitura do ledger até o último `/v1/usage`, `--model` fixado no pre-route (recusa 4xx = erro; Decision antigo
    que ignora o pin = `model_override: ignored`);
  - Decision fora do ar, juiz, mídia pulada, `requires: [edge]` (sonda via `SendEnv`, sshd sem `AcceptEnv`);
  - SSH com `ssh` falso, bytes NUL removidos antes do Postgres.
- **Integração (Postgres real):** ida e volta no Postgres, a view comparada com o relatório, `session_id`
  duplicado e linha com NUL na saída.

## Limitações conhecidas

- **`--model`:** o bench envia o modelo no pre-route (`/v1/route` com `model`), que vira o modelo inicial da run
  (`route_reason = "pinned by caller"`); o escalonamento continua pela escada, limitado pelo `max_tier` do agente.
  Um modelo acima do `max_tier` (ou fora do `routing.yaml`) é recusado com 400, e a linha vira erro, não uma run sem
  o modelo pedido. Com o Decision fora do ar, o plugin falha aberto e o Hermes usa o `model` do próprio `/v1/runs`.
  Se o modelo servido ainda assim for outro, a linha recebe `check_detail.model_override = "ignored"`.
- **Perfil sob multiplexação:** o gateway multiplexado (padrão na v2026.9.24) serve `/p/<perfil>/` no processo do
  chief. O plugin usa `hermes_cli.profiles.current_profile_name()` (o `HERMES_HOME` do escopo da requisição), então
  cada run é avaliada com a política do perfil servido.
- **Gate em Python:** cada fixture Python tem um `Makefile` com `test: python3 -m unittest -q` (protegido no
  check). É a detecção de maior prioridade do `aios-check` e não baixa nada, então o gate recebe evidência
  determinística de pass/fail, como em Go e TS, em vez de `validation: none`.
