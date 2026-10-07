# agent-system — AI Agent OS (V1 local)

Sistema operacional pessoal de agentes, conforme [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).
V1 roda inteiro nesta máquina via Docker Compose. Tudo é endereçado por nome de serviço + variáveis
de ambiente, então a migração para Railway (Fase 7) troca só a infraestrutura.

```
USER → Hermes (runtime de agentes) → LiteLLM (gateway, tiers 2–7) → provedores
              │                         └── Ollama local (Qwen3 4B, embeddings) no host, Vulkan/780M
              ├── Decision Service (Go): rules → Qwen local → Jev → OpenAI Decisions
              └── Knowledge: Postgres 17 + pgvector (documents/chunks/memories), workers/kb
```

## Componentes

| Serviço | O quê | Porta (só 127.0.0.1) | Profile |
|---|---|---|---|
| postgres | pgvector 0.8.7 / PG17 — DBs `aios`, `litellm`, `langfuse` | 5432 | core |
| valkey | cache/filas/locks (Redis-compatível) | — | core |
| litellm | gateway de modelos, budgets, spend logs, fallbacks | 4000 | core |
| decision | Decision Service / Jev (`decision/`) | 8090 | core |
| hermes | Hermes Agent (Nous Research) gateway + API OpenAI-compatível | 8642 | agent |
| whisper | whisper.cpp Vulkan (transcrição pt-BR) | 8178 | media |
| langfuse-* | Langfuse v4 self-hosted + ClickHouse + MinIO | 3000 | observability |
| Ollama | host (systemd), `qwen3:4b-instruct-2507-q4_K_M`, `qwen3-embedding:0.6b` | 11434 | host |

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
| 7 | `tier7-fable` | Claude Fable 5.1 (só com aprovação) | 10/50 |

`make litellm-config` gera `config/litellm/config.yaml` só com provedores cuja chave está no `.env`.
Cada deployment tem rota direta (order 1) e OpenRouter como backup (order 2).

## Orçamento de RAM (16 GB, ~5 GB livres hoje)

| Sempre ligado | ~RAM | Sob demanda | ~RAM |
|---|---|---|---|
| postgres (cap 1 GB) | 0.4–0.7 GB | Qwen3 4B (Ollama, 4K ctx) | ~2.9 GB |
| litellm (cap 1.5 GB) | 0.7–1.2 GB | qwen3-embedding 0.6B | ~0.9 GB |
| hermes (cap 1.25 GB) | 0.35–0.6 GB | whisper large-v3-turbo q5 | ~1.1 GB |
| valkey + decision | <0.1 GB | Langfuse self-host | 1.6–2.5 GB |

`OLLAMA_MAX_LOADED_MODELS=1` + `KEEP_ALIVE=5m`: um modelo local por vez. Observabilidade padrão
= spend logs do LiteLLM (Postgres). Langfuse: Cloud (só preencher `LANGFUSE_*`) ou `make obs-up` quando houver RAM.
Com 32 GB: observability sempre ligado, Qwen3 8B, `MAX_LOADED_MODELS=2`.

## Setup (uma vez)

```bash
# 1. Pacotes de sistema (revise o script antes!) — Docker, ffmpeg, jq, gh, psql, Ollama (Vulkan)
sudo bash infra/scripts/bootstrap-host.sudo.sh
#    → faça LOGOUT/LOGIN (grupos docker e render)

# 2. Verificar o host
make doctor

# 3. Segredos + chaves de provedores
make env                      # já gera todos os segredos internos
$EDITOR .env                  # cole ANTHROPIC/OPENROUTER/MOONSHOT/DEEPSEEK/OPENAI/TYPESAFE que tiver

# 4. Modelos locais
make models whisper-model

# 5. Subir
make up-core                  # postgres → migrations → litellm → virtual keys → decision
make up                       # + Hermes
make smoke                    # chat local, embeddings (1024), decisão, Hermes
make stats                    # RAM real → atualize a tabela acima

# 6. Rotina
make timers-install           # backup 03:00 + restart do Hermes 04:00 (systemd --user, Persistent)
```

## Uso diário

```bash
make hermes-shell                                   # conversar com o Chief (Hermes)
make kb-ingest tenant=pessoal path=~/Obsidian/Pessoal
make kb-search tenant=pessoal q="metas de estudo 2026"
make transcribe f=knowledge/inbox/aula.m4a          # → .txt/.srt ao lado
make backup | make logs s=litellm | make psql | make help
```

## Segurança

- Todas as portas publicadas só em `127.0.0.1` (compose + `daemon.json "ip"`). Ollama escuta `0.0.0.0`,
  mas o ufw só libera a sub-rede do compose (`172.30.0.0/24`); o bootstrap aborta se o ufw não negar entrada.
- Hermes, decision e kb usam **virtual keys** do LiteLLM com orçamento e allowlist de modelos — nunca a master key.
  `tier7-fable` não está na chave do Hermes.
- A API do Hermes (8642) dá acesso a terminal: exige `API_SERVER_KEY`, nunca exponha.
- Isolamento Nitro (trabalho) × Pessoal: coluna `tenant` em tudo; o `kb` recusa ingerir o vault Nitro em
  outro tenant (e vice-versa); o Hermes **não** monta `~/Obsidian`.
- `.env` (600) e `backups/` são gitignored. Não coloque este repo nem `backups/` em pasta sincronizada.
- `LITELLM_SALT_KEY` nunca pode mudar depois da primeira chave criada — o backup copia o `.env`.

## Estrutura

```
agents/<domínio>/{agent.yaml,SOUL.md}   chief, engineering, finance, projects, personal, learning
skills/<categoria>/<skill>/SKILL.md     montado read-only no Hermes (/opt/shared-skills)
config/{litellm,hermes,decision}/       templates de configuração
decision/                               Decision Service (Go) — ver decision/README.md
workers/kb/                             ingestão + busca híbrida (Python/uv) — ver workers/kb/README.md
migrations/                             schema versionado (banco vazio = migrations)
infra/{scripts,docker,systemd,railway}/ bootstrap, backup, migrate, timers
docs/                                   arquitetura + pesquisas verificadas (2026-10-07)
```

## Fases (§27)

1. **Ambiente local** ← este repo
2. Foundation dos agentes: hooks, permissions, budgets, coding loops
3. Knowledge: retrieval, Context Compiler, memória, eventos (o schema e `workers/kb` já existem)
4. Outros domínios · 5. Automação (cron/webhooks/reviews) · 6. Otimização · 7. Railway
