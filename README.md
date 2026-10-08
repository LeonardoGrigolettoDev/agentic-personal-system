# agent-system — AI Agent OS (V1 local)

Sistema operacional pessoal de agentes, conforme [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) (visão) e
[`docs/CONTRACTS.md`](docs/CONTRACTS.md) (contratos implementados). A V1 roda inteira nesta máquina via Docker Compose;
tudo é endereçado por nome de serviço + variáveis de ambiente, e o kit de migração para o Railway (V2) já está em
`infra/railway/`. Operação do dia a dia: [`docs/RUNBOOK.md`](docs/RUNBOOK.md).

```
USER (CLI · Telegram · API · cron)
  └─ Hermes (runtime: agentes/perfis, sessões, skills, cron, hooks, ferramentas, Kanban, execução)
       │  plugin aios (nos gates de cada sessão)
       ├─ Decision Service (Go) ── Jev / rules / Qwen local / OpenAI: decisões tipadas,
       │     roteamento de modelo, orçamento (ledger de custo), escalonamento (done/repair/escalate/fail)
       ├─ knowledge (MCP + HTTP) ─ Postgres 17 + pgvector: documentos, memória em camadas, Context Compiler
       ├─ LiteLLM ──────────────── tiers 2–7 (diretos + OpenRouter) · Ollama local no host (Vulkan/780M)
       ├─ sandbox (SSH) ────────── terminal isolado dos agentes: git, Go, Node, Python, aios-check
       └─ edge ─────────────────── mídia local: ffmpeg → whisper.cpp → resumo local → knowledge
```

**Divisão de responsabilidades:** o Hermes é o único runtime de agentes. Go existe só no Decision Service
(integração com o Jev, tipagem das decisões, políticas de roteamento, orçamento e escalonamento). Nenhum outro
componente roda um loop de agente.

## Componentes

| Serviço | O quê | Porta (só 127.0.0.1) | Profile |
|---|---|---|---|
| postgres | pgvector 0.8.7 / PG17 — DBs `aios`, `litellm`, `langfuse` | 5432 | core |
| valkey | cache/filas/locks (Redis-compatível) | — | core |
| litellm | gateway de modelos, chaves virtuais com orçamento, spend logs, fallbacks | 4000 | core |
| decision | Decision Service (`decision/`, Go): `/v1/decide`, `/v1/route`, `/v1/usage`, `/v1/gate`, aprovações, relatórios | 8090 | core |
| knowledge | base de conhecimento + memória + Context Compiler (`workers/kb/`), MCP em `/mcp` | 8092 | core |
| hermes | Hermes Agent v2026.9.24: gateway do chief (multiplexa os perfis), API, cron, dispatcher Kanban | 8642 | agent |
| sandbox | execução isolada (sshd, `/workspace`), só na rede `sandbox_net` (`workers/sandbox/`) | — | agent |
| whisper | whisper.cpp Vulkan (pt-BR) | 8178 | media |
| edge | worker de mídia: upload → transcrição → resumo → ingestão (`workers/edge/`) | 8093 | media |
| bench | bateria de testes da V1 (`bench/`, 55 tarefas), one-shot | — | bench |
| langfuse-* | Langfuse v4 self-hosted + ClickHouse + MinIO | 3000 | observability |
| Ollama | host (systemd): `qwen3:4b-instruct-2507-q4_K_M`, `qwen3-embedding:0.6b` | 11434 | host |

### Agentes (perfis do Hermes)

| Perfil | Papel | Modelo inicial | Shell |
|---|---|---|---|
| chief | orquestra: conversa, revisões agendadas, delega pelo Kanban | roteado (tier 2–3) | não |
| engineering | código, debugging, review, testes (no sandbox) | `tier3-code` | sim |
| finance | planilhas, gastos, orçamento (só analisa) | `tier2-flash` | sim |
| projects | status, roadmap, decisões | `tier2-flash` | não |
| personal | rotina e organização (só `pessoal`) | `tier2-cheap` | não |
| learning | estudos, resumos, revisão espaçada | `tier2-cheap` | não |

Política de cada um em `agents/<perfil>/agent.yaml` (categorias de ferramenta, tenants, `max_tier`, orçamento);
personalidade em `SOUL.md`. Os domínios rodam sob demanda como workers do Kanban, um por vez.

### Revisões agendadas (cron do chief)

07:30 morning-review · 08:00 daily-planning (seg–sex) · 09:00 engineering-review (seg–sex) · 18:30 project-review
(seg–sex) · 20:00 learning-review · 22:30 daily-reflection · dom 19:00 weekly-review · sex 17:30 weekly-review-nitro.
Cada execução fica num único tenant (`nitro` = trabalho, `pessoal` = vida pessoal). Prompts em `workflows/`,
19 skills em `skills/`.

## Tiers de modelo (aliases LiteLLM — §13)

| Tier | Alias | Modelo | US$/M in/out |
|---|---|---|---|
| 0 | — | hooks, git, testes, SQL | 0 |
| 1 | Decision Service | rules → `decider-local` → **Jev** (`jev-latest`, TypeSafe) → OpenAI Decisions | 0 / 0.042 / 0.10 |
| 2 | `tier2-cheap`, `tier2-flash`, `local-qwen` | Qwen3.7 Flash, DeepSeek V4.1 Flash, Qwen3 4B local | 0.03/0.13, 0.30/1.20, 0 |
| 3 | `tier3-code` | Kimi K2.7 Code | 0.95/4 |
| 4 | `tier4-pro`, `tier4-k3` | DeepSeek V4 Pro, Kimi K3 | 1.32/3.96, 3/15 |
| 5 | `tier5-sonnet` | Claude Sonnet 5.5 | 2/10 |
| 6 | `tier6-opus` | Claude Opus 5.5 | 4/20 |
| 7 | `tier7-fable` | Claude Fable 5.1 (só com aprovação humana) | 10/50 |

- O modelo de cada chamada vem do Decision Service. O plugin `aios` reescreve o `model` de cada chamada:
  `config/routing.yaml` (tipo de tarefa × complexidade, depois estatísticas aprendidas de custo por sucesso),
  limitado pelo `max_tier` do agente.
- Duas falhas seguidas no mesmo tier sobem um degrau. Acima do `max_tier`, o sistema pede aprovação
  (Telegram/ntfy + `make approvals`).
- `make litellm-config` gera `config/litellm/config.yaml` só com os provedores que têm chave no `.env`. Cada
  deployment tem rota direta (order 1) e OpenRouter como backup (order 2).

## Orçamento de RAM (16 GB)

| Sempre ligado | ~RAM | Sob demanda | ~RAM |
|---|---|---|---|
| postgres (cap 1 GB) | 0.4–0.7 GB | Qwen3 4B (Ollama, 4K ctx) | ~2.9 GB |
| litellm (cap 1.5 GB) | 0.7–1.2 GB | qwen3-embedding 0.6B | ~0.9 GB |
| hermes (cap 2 GB: chief + 1 worker) | 0.4–1.2 GB | whisper large-v3-turbo q5 + edge | ~1.1 + 0.3 GB |
| sandbox (cap 2 GB, ocioso ~50 MB) | <0.1 GB | Langfuse self-host | 1.6–2.5 GB |
| valkey + decision + knowledge | ~0.3 GB | | |

`OLLAMA_MAX_LOADED_MODELS=1` + `KEEP_ALIVE=5m`: um modelo local por vez. A observabilidade padrão são os spend
logs do LiteLLM e os relatórios do Decision Service (`make report`). Para o Langfuse, use a Cloud (basta preencher
`LANGFUSE_*`) ou `make obs-up` quando houver RAM. Com 32 GB: observabilidade sempre ligada e Qwen3 8B.

## Setup (uma vez)

```bash
# 1. Pacotes de sistema (revise o script antes!): Docker, ffmpeg, jq, gh, psql, Ollama (Vulkan), ufw
sudo bash infra/scripts/bootstrap-host.sudo.sh
#    → faça LOGOUT/LOGIN (grupos docker e render)

# 2. Verificar o host
make doctor

# 3. Segredos + chaves de provedores
make env                      # gera todos os segredos internos (inclui chaves de edge por tenant e por perfil)
$EDITOR .env                  # cole ANTHROPIC/OPENROUTER/MOONSHOT/DEEPSEEK/OPENAI/TYPESAFE que tiver

# 4. Modelos locais
make models whisper-model

# 5. Subir
make up-core                  # postgres → migrations → litellm → chaves virtuais → decision + knowledge
make up                       # + sandbox + Hermes; aplica perfis, plugin, segredos e crons (make hermes-setup)
make smoke                    # chat local, embeddings (1024), decisão, Hermes
make hermes-doctor            # hermes doctor + config check + plugin aios
make stats                    # RAM real

# 6. Rotina
make timers-install           # backup 03:00 + restart do Hermes 04:00 (systemd --user)
make edge-up                  # opcional: whisper + edge (mídia)
```

## Uso diário

```bash
make hermes-shell                                    # conversar com o chief
make hermes-kanban                                   # tarefas dos agentes de domínio
make approvals / make approve id=<id>                # escalonamentos que pedem humano
make report r=costs|models|agents|loops|routes|escalations|spend
make kb-ingest tenant=pessoal path=~/Obsidian/Pessoal
make kb-project tenant=nitro slug=app-x name="App X" repo=git@github.com:empresa/app-x.git
make kb-search tenant=pessoal q="metas de estudo 2026"
make edge-pipeline f=~/Downloads/aula.m4a tenant=pessoal domain=learning title="Aula 3"
make bench-run category=debugging                    # bateria da V1 → make bench-report
make backup | make logs s=litellm | make psql | make help
```

## Segurança

- Todas as portas publicadas ficam só em `127.0.0.1`. O Ollama escuta `0.0.0.0`, mas o ufw só libera a sub-rede
  `aios` (`172.30.0.0/24`). O sandbox fica numa rede separada (`sandbox_net`, `172.30.1.0/24`), compartilhada só
  com hermes, edge e bench: código rodado pelos agentes não alcança Ollama, whisper, Postgres nem LiteLLM.
- O sandbox não tem `docker.sock` nem montagens do host além de `data/sandbox/*`, e roda com `cap_drop: ALL` mais
  o mínimo que o sshd precisa.
- O plugin `aios` aplica em toda chamada de ferramenta:
  - as permissões do agente;
  - o isolamento de tenant (`nitro` × `pessoal`; `shared` é legível por todos);
  - bloqueio de arquivos de segredo (`.env`, chaves, credenciais);
  - bloqueio de comandos destrutivos.
  Sessões de teste (`bench-*`) não gravam memória nem criam tarefas.
- Cada serviço usa sua **chave virtual** do LiteLLM, com orçamento e allowlist de modelos, nunca a master key.
- O edge tem uma chave admin (só host) e chaves por tenant. Um worker do Kanban recebe apenas a chave do tenant da
  sua tarefa.
- A API do Hermes (8642) dá acesso a terminal: exige `API_SERVER_KEY`. Nunca a exponha.
- O `kb` recusa ingerir o cofre Nitro em outro tenant (e vice-versa), e o Hermes não monta `~/Obsidian`.
- `.env` (600) e `backups/` ficam fora do git. Não coloque o repo nem `backups/` em pasta sincronizada.
  `LITELLM_SALT_KEY` nunca pode mudar depois da primeira chave criada.

## Estrutura

```
agents/<perfil>/{agent.yaml,SOUL.md}    chief, engineering, finance, projects, personal, learning
skills/<categoria>/<skill>/SKILL.md     19 skills (montadas read-only no Hermes) + scripts testados
workflows/*.md                          prompts das revisões agendadas
config/{litellm,hermes,decision}/       templates; config/routing.yaml = política de roteamento e preços
tools/hermes-plugin/aios/               plugin do Hermes: roteamento, uso, permissões, gate, contexto
decision/                               Decision Service (Go)
workers/kb/                             knowledge: ingestão, busca híbrida, memória, Context Compiler, MCP
workers/sandbox/                        imagem do sandbox + aios-check/aios-task-* (validação, patches)
workers/edge/                           worker de mídia (ffmpeg, whisper, resumo map-reduce, retenção)
bench/                                  bateria da V1 (tarefas, fixtures, runner, relatório)
infra/{scripts,docker,hermes,systemd}/  bootstrap, env, migrate, backup, setup do Hermes, timers
infra/railway/                          kit da V2 (Railway, edge-gw via Tailscale, backup em bucket)
migrations/                             schema versionado (001–006 core/decision, 013 bench)
mk/*.mk                                 alvos make por componente
docs/                                   arquitetura, contratos, runbook, pesquisas verificadas (2026-10-07)
```

## Testes

`make test` roda todas as suítes: decision (Go), knowledge, plugin do Hermes, skills, sandbox, edge, bench e o kit
do Railway. As suítes com banco usam o Postgres quando há URL de teste (`make test-integration`, `BENCH_TEST_DATABASE_URL`, `KB_TEST_DATABASE_URL`).
O plugin, o `hermes-setup`, as skills e os crons também foram validados contra o Hermes v2026.9.24 real:
- gateway multiplexado com dispatcher Kanban;
- workers de perfil;
- MCP do knowledge;
- isolamento de tenant.

**Ainda não validado nesta máquina:** a subida completa com Docker (build das imagens, `make up`, `make smoke`).
Depende do `bootstrap-host.sudo.sh`.

## Fases (§27)

1–5 implementadas (ambiente, fundação dos agentes, knowledge, domínios, automação). A 6 (otimização) está
implementada: roteamento aprendido, relatórios, bench; os números vêm do uso real. A 7 (Railway) está preparada
em `infra/railway/` e executável pelo RUNBOOK §9.
