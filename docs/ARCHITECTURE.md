# AI Agent Operating System — Referência de Arquitetura

**Status:** arquitetura conceitual para implementação
**Data:** 2026-10-07

## 1. Objetivo

Construir um ambiente pessoal de agentes de IA capaz de executar trabalho real de forma contínua, cobrindo:

- trabalho profissional, principalmente programação;
- finanças e análise de planilhas;
- empresa e projetos pessoais;
- organização pessoal;
- estudos e metas;
- pesquisas e automações.

A meta não é apenas criar vários chatbots. É criar um **sistema operacional pessoal de agentes**, com foco em:

```text
regras determinísticas
+ decision engine barato
+ retrieval
+ contexto enxuto
+ skills
+ hooks
+ loops controlados
+ roteamento de modelos
+ escalonamento
+ observabilidade
```

### Tese central

> Não queremos usar um LLM caro para fazer tudo. Queremos construir um sistema que escolha cuidadosamente quando cada LLM precisa trabalhar.

A métrica principal é **custo por tarefa concluída com sucesso**, e não apenas preço por milhão de tokens.

---

## 2. Arquitetura geral

```text
                         USUÁRIO
                            │
                            ▼
                    ┌──────────────┐
                    │  INTAKE/API  │
                    └──────┬───────┘
                           │
                           ▼
                 ┌────────────────────┐
                 │ DECISION ENGINE    │
                 │ Rules + Jev        │
                 └─────────┬──────────┘
                           │
          ┌────────────────┼────────────────┐
          ▼                ▼                ▼
       domínio        complexidade       política
                           │
                           ▼
                    MEMORY / RETRIEVE
                           │
                           ▼
                    CONTEXT COMPILER
                           │
                           ▼
                     MODEL ROUTER
                           │
            ┌──────────────┼──────────────┐
            ▼              ▼              ▼
          cheap          medium         frontier
                           │
                           ▼
                    AGENT + SKILLS
                           │
                           ▼
                    TOOLS / WORKSPACE
                           │
                           ▼
                       HOOKS/TESTS
                           │
                           ▼
                       VALIDATOR
                           │
                           ▼
                    DECISION GATE
                     │    │     │
                   DONE REPAIR ESCALATE
```

### Princípios

1. Determinístico primeiro.
2. O LLM não recebe toda a memória.
3. Estado fica fora do LLM.
4. Modelo caro só entra quando a evidência justificar.
5. Toda tarefa possui limite de tokens, custo e iterações.
6. O sistema deve aprender, com o uso, quais rotas resolvem melhor cada tipo de tarefa.

---

## 3. Hermes

Hermes será o runtime principal dos agentes:

- sessões;
- cron/jobs;
- skills;
- hooks;
- tool execution;
- subagentes;
- comunicação;
- execução no workspace.

Hermes não será a fonte definitiva de memória ou billing.

---

## 4. Agentes por domínio

Não criar um agente permanente para cada projeto. Usar agentes por domínio:

```text
agents/
├── chief
├── engineering
├── finance
├── projects
├── personal
└── learning
```

Projetos são contexto:

```text
engineering
 ├── trabalho
 ├── mini-ERP
 ├── visão computacional
 └── outros
```

Isso evita uma explosão de agentes.

### Chief / Orchestrator

Coordena tarefas cruzadas. Exemplo:

```text
agenda
→ tarefas
→ projetos
→ estudos
→ delegação
→ consolidação
→ resposta
```

---

## 5. Skills

Capacidades ficam nas skills, carregadas sob demanda:

```text
skills/
├── coding/
│   ├── repository_analysis
│   ├── debugging
│   ├── code_review
│   ├── architecture_review
│   └── test_generation
├── finance/
├── research/
├── projects/
├── productivity/
└── knowledge/
```

Princípio: **skill sob demanda, não contexto permanente gigante**.

---

## 6. Decision Engine e Jev

Jev será uma peça central do sistema, principalmente para decisões fechadas e estruturadas.

Interface recomendada:

```go
type DecisionEngine interface {
    Decide(ctx context.Context, state State, questions []Question) DecisionResult
}
```

Implementações possíveis:

```text
DecisionEngine
├── Jev
├── OpenAI Decisions
└── local classifier / rules
```

### O que o Decision Engine decide

- domínio;
- agente;
- skill;
- ferramenta;
- complexidade;
- modelo;
- prioridade;
- necessidade de pesquisa;
- necessidade de confirmação;
- continuar;
- reparar;
- escalar;
- terminar.

### Jev dentro do workflow

Não usar Jev depois de cada ferramenta. Usar em **gates**:

```text
TASK
 ↓
JEV → ROUTE
 ↓
PLAN
 ↓
EXECUTE
 ↓
VALIDATE
 ↓
JEV → CONTINUE / REPAIR / ESCALATE / DONE
```

---

## 7. Workflow de desenvolvimento

A meta é reproduzir as boas propriedades do Claude Code por arquitetura, não por dependência de um único modelo.

```text
TASK
 ↓
DECIDE
 ↓
PLAN
 ↓
IMPLEMENT
 ↓
TEST
 ├── PASS → REVIEW → DONE
 └── FAIL → DIAGNOSE → REPAIR → TEST
```

### Planning loop

```text
objetivo
 ↓
entender repository
 ↓
identificar arquivos/dependências
 ↓
plano
```

### Tool loop

```text
LLM
 ↓
tool
 ↓
resultado
 ↓
LLM
 ↓
tool
 ↓
resultado
```

### Test/Repair loop

```text
IMPLEMENT
 ↓
TEST
 ↓
FAIL
 ↓
DIAGNOSE
 ↓
REPAIR
 ↓
TEST
```

### Reviewer loop

```text
implementer
 ↓
reviewer
 ↓
pass/fail
```

### Escalation loop

```text
cheap
 ↓
medium
 ↓
strong
 ↓
frontier
```

O loop deve escalar por evidência: falhas repetidas, baixa confiança, grande mudança arquitetural, alta criticidade ou gasto excessivo.

---

## 8. Hooks

Tudo que é determinístico deve ser hook, não pensamento do LLM.

Exemplos:

```text
post_edit → gofmt
post_edit → ruff
post_edit → eslint
post_edit → tsc
pre_tool → bloquear .env
post_test → registrar resultado
stop → notificar usuário
```

Objetivos:

- zero tokens;
- baixa latência;
- segurança;
- previsibilidade.

---

## 9. Context Compiler

```text
Knowledge Base
 ↓
Retriever
 ↓
documentos candidatos
 ↓
relevance filter
 ↓
Context Compiler
 ↓
Task Context
 ↓
LLM
```

Objetivo: transformar memória grande em contexto pequeno e relevante.

Exemplo:

```json
{
  "task": "review architecture",
  "project": "mini-erp",
  "current_state": "...",
  "relevant_decisions": ["..."],
  "relevant_files": ["..."],
  "constraints": ["..."]
}
```

---

## 10. Knowledge Base

Não usar uma vector database como única fonte de verdade.

```text
                    KNOWLEDGE SYSTEM
                           │
             ┌─────────────┼─────────────┐
             ▼             ▼             ▼
        STRUCTURED      DOCUMENTS      EVENTS
             │             │             │
             ▼             ▼             ▼
        PostgreSQL      R2/Bucket      PostgreSQL
        + pgvector
```

### PostgreSQL

Projetos, tarefas, agentes, permissões, eventos, memórias, documentos, embeddings, custos e execuções.

### pgvector

Busca vetorial dentro do próprio PostgreSQL.

### Object storage

PDFs, planilhas, documentos, áudio, vídeo, artefatos e backups.

### Redis

Cache, filas leves, locks, rate limiting e estado transitório.

---

## 11. Memória

### Global

```text
preferences
working style
communication
rules
```

### Domínio

```text
engineering
finance
projects
personal
learning
```

### Projeto

```text
description
architecture
decisions
roadmap
tasks
issues
repository
constraints
```

### Eventos

Registrar decisões e mudanças importantes.

### Memória episódica

Guardar somente o que tem valor futuro:

```text
temporary
important
persistent
deprecated
```

Não transformar cada conversa em memória permanente.

---

## 12. Permissões

Cada agente deve possuir escopo explícito de dados e ferramentas.

```yaml
agent: engineering

allowed_domains:
  - engineering
  - projects

deny_domains:
  - finance

allowed_tools:
  - github
  - filesystem
  - shell
  - docker

max_cost_per_run: 0.50
```

Princípio: **acesso mínimo necessário**.

---

## 13. Model routing

A escolha deve considerar capacidade, custo esperado e probabilidade de sucesso.

### Tier 0 — determinístico

Git, AST, testes, lint, parsers, SQL, cache.

### Tier 1 — decisão

Jev, OpenAI Decisions ou classificador local.

### Tier 2 — cheap

DeepSeek Flash/Qwen small e equivalentes.

Uso:
- classificação;
- extração;
- resumo;
- transformações simples;
- correções triviais.

### Tier 3 — coding worker

**Kimi K2.7 Code**.

Uso:
- implementação cotidiana;
- navegação de repository;
- edição multi-arquivo;
- debugging moderado;
- test repair;
- tool use.

### Tier 4 — strong

**DeepSeek V4 Pro / Kimi K3**.

Uso:
- arquitetura;
- debugging difícil;
- repositories grandes;
- tarefas agentic longas;
- raciocínio pesado.

### Tier 5 — frontier eficiente

**Claude Sonnet**.

Uso:
- falha de K2.7/K3;
- tarefa crítica;
- boa combinação de reasoning + tool use;
- necessidade de convergência rápida.

### Tier 6 — frontier forte

**Claude Opus / GPT frontier**.

Uso:
- arquitetura muito complexa;
- long-running coding;
- debugging persistente;
- segunda opinião independente.

### Tier 7 — último recurso

**Claude Fable / equivalente máximo**.

Uso excepcional em problemas muito difíceis ou críticos.

---

## 14. Cost per successful task

Registrar:

```text
task_type
model
input_tokens
output_tokens
cache_hits
iterations
tools_used
time
success
retries
final_cost
```

A métrica principal será:

```text
custo por tarefa concluída
```

Exemplo:

```text
K2.7
$0,25 × 4 tentativas = $1,00

Sonnet
$0,80 × 1 tentativa = $0,80
```

Nesse caso, Sonnet é economicamente superior.

---

## 15. Token budgets

Cada task deve possuir:

```yaml
task:
  token_budget:
    total: 30000
    decision: 1000
    retrieval: 3000
    execution: 20000
    final: 6000
```

Também:

```text
max iterations
max cost
deadline
```

---

## 16. Cache

Organizar o contexto para manter prefixo estável:

```text
[STATIC SYSTEM]
[STATIC AGENT POLICY]
[STATIC SKILLS]
[STATIC TOOLS]
-----------------
[DYNAMIC MEMORY]
[DYNAMIC TASK]
[DYNAMIC TOOL RESULTS]
```

Isso favorece cache e reduz custo.

---

## 17. Cron, eventos e manual

```text
TIME   → CRON
EVENT  → WEBHOOK/EVENT
USER   → MANUAL REQUEST
```

Todos viram uma `Task` comum.

Exemplos:

```text
07:30 → Morning Review
08:00 → Daily Planning
09:00 → Engineering Review
18:30 → Project Review
20:00 → Learning Review
22:30 → Daily Reflection
```

---

## 18. Observabilidade

Usar Langfuse ou equivalente para registrar:

- trace;
- modelo;
- tokens;
- cache;
- tool calls;
- retrieval;
- latência;
- custo;
- success/fail;
- escalation.

Perguntas que o sistema deve responder:

- qual agente custa mais?
- qual modelo resolve mais?
- qual skill gera mais tokens?
- qual workflow entra mais em loop?
- qual rota possui melhor custo/sucesso?
- quando estamos escalando demais?

---

## 19. LiteLLM

Arquitetura:

```text
Hermes
  ↓
LiteLLM
 ├── OpenRouter
 ├── Anthropic
 ├── OpenAI
 ├── Kimi
 ├── DeepSeek
 └── outros
```

Funções:

- interface unificada;
- routing;
- fallback;
- retries;
- budgets;
- spend tracking.

OpenRouter continua útil como catálogo, discovery e provider routing, mas a arquitetura não pode depender dele.

---

## 20. Hospedagem

### V1: Railway como control plane

Railway é uma opção adequada para o núcleo do sistema porque já fornece persistent services, cron jobs, PostgreSQL, Redis, volumes, buckets, private networking, secrets e integração Git.

Arquitetura:

```text
Railway
│
├── Hermes
├── LiteLLM
├── Decision Service
├── PostgreSQL
├── Redis
├── Langfuse
└── Bucket
```

### Execution plane futuro

Se coding agents precisarem de isolamento maior:

```text
Railway = control plane

Hetzner / Daytona / Modal / Docker / SSH
= execution plane
```

Não adicionar isso antes de existir necessidade real.

---

## 21. Coding workers isolados

```text
Control Plane
     ↓
Task Queue
     ↓
Isolated Worker
     ├── clone repo
     ├── modify
     ├── test
     ├── produce patch
     └── destroy
```

Princípio: o agente que decide não precisa ser o mesmo processo que executa código arbitrário.

---

# 22. IA local para transcrição e mídia

O notebook com **Ryzen 7 250 + 16 GB RAM, futuramente 32 GB + 500 GB SSD** é adequado para ser um **worker local especializado em mídia**, sem tentar substituir o motor cloud principal.

A ideia:

```text
Local AI
├── transcription
├── audio processing
├── video extraction
├── summarization
└── private/offline processing
```

## 22.1 Transcrição

Recomendação principal:

```text
Whisper / whisper.cpp
```

Alternativa:

```text
faster-whisper
```

Uma opção de alta qualidade é `Whisper large-v3-turbo`. O modelo oficial cobre 99 idiomas; a distribuição publicada no Hugging Face tem cerca de 1,62 GB para os pesos e foi reduzida para quatro camadas de decoding, buscando ficar significativamente mais rápida que o large-v3 original com pequena perda de qualidade. citeturn754201search0turn754201search6

### Recomendações práticas

Com 16 GB:

```text
Whisper small/medium
ou
large-v3-turbo com configuração conservadora
```

Com 32 GB:

```text
large-v3-turbo
```

A velocidade real deve ser medida na própria máquina antes de definir o padrão.

---

## 22.2 Resumo local

Usar Ollama com Qwen3.

A família Qwen3 disponibiliza variantes de 0,6B, 1,7B, 4B, 8B, 14B, 30B, 32B e 235B. citeturn754201search1

### 16 GB

Começar com:

```text
Qwen3 4B
```

Ollama possui, por exemplo, `qwen3:4b-instruct`. citeturn754201search3

Uso:

- resumo de transcrição;
- tópicos;
- tarefas extraídas;
- notas;
- classificação;
- limpeza de texto.

### 32 GB

Testar:

```text
Qwen3 8B
```

E, se a velocidade for aceitável:

```text
Qwen3 14B quantizado
```

Não é necessário começar grande.

---

## 22.3 Pipeline local de áudio/vídeo

```text
AUDIO / VIDEO
     │
     ▼
   FFmpeg
     │
     ▼
Audio extraction
     │
     ▼
Whisper local
     │
     ▼
transcript + timestamps
     │
     ▼
Qwen local
     │
     ▼
summary / notes / tasks
```

Serve para:

- aulas;
- reuniões;
- vídeos;
- áudio de WhatsApp;
- entrevistas;
- gravações;
- documentação falada.

---

## 22.4 Diarização

Fase futura:

```text
audio
 ↓
Whisper
 ↓
speaker diarization
 ↓
speaker-labeled transcript
 ↓
Qwen summary
```

Útil para reuniões e entrevistas. Deve ser adicionada só depois de o pipeline de transcrição estar estável.

---

## 22.5 Worker local

O ThinkPad pode rodar um serviço `agent-edge`:

```text
/transcribe
/summarize
/extract_audio
/process_video
/embed
```

A arquitetura seria:

```text
Railway
   ↓
Task
   ↓
VPN/Tailscale
   ↓
ThinkPad
   ↓
Local AI
   ↓
resultado
   ↓
Railway
```

Assim o notebook vira um **worker especializado**.

---

## 23. Armazenamento local

Com 500 GB:

```text
models/
whisper/
qwen/
cache/

media/
input/
processing/
archive/
```

Aplicar retenção para vídeos originais e arquivos temporários.

Manter, quando possível:

```text
original
→ transcript
→ summary
```

---

# 24. Setup local — V1 recomendada

## 24.1 Objetivo do setup local

No estágio atual, a recomendação é **não hospedar ainda o control plane no Railway**.

A V1 deve rodar integralmente no computador local, usando Docker Compose, enquanto validamos:

- agentes;
- skills;
- loops;
- Jev;
- routing de modelos;
- custos;
- memória;
- coding workflows;
- IA local;
- integrações.

O objetivo é desenvolver o sistema como se ele fosse produção, mas sem assumir custo e complexidade de uma infraestrutura 24/7 antes de existir necessidade real.

## 24.2 Princípio de portabilidade

Tudo deve ser configurável por ambiente:

```text
APPLICATION
  ≠
INFRASTRUCTURE
```

A aplicação deve enxergar serviços por URLs e variáveis de ambiente, e não por caminhos ou hosts hardcoded.

Localmente:

```text
Docker Compose
```

Posteriormente:

```text
Railway Services
```

A lógica dos agentes não deve depender de onde os containers estão executando.

## 24.3 Stack local inicial

```text
Seu computador
│
├── Docker / Docker Compose
│
├── Hermes
├── LiteLLM
├── Decision Service / Jev
├── PostgreSQL + pgvector
├── Redis
├── Langfuse
│
├── Ollama
│   ├── Qwen3 4B/8B
│   └── outros modelos locais
│
├── Whisper / faster-whisper / whisper.cpp
├── FFmpeg
│
└── Coding Workspaces / Workers
```

## 24.4 Hardware alvo

O ThinkPad Ryzen 7 250 com 16 GB de RAM já é suficiente para iniciar a V1.

Com 32 GB, teremos uma margem melhor para:

- Postgres + Redis + serviços auxiliares;
- containers de desenvolvimento;
- Qwen local maior;
- processamento de áudio/vídeo;
- múltiplas ferramentas simultâneas.

Não assumir que modelos grandes rodarão bem em CPU apenas pelo fato de caberem na RAM. Fazer benchmark real.

## 24.5 Pré-requisitos

Instalar: 

```text
Docker
Docker Compose Plugin
Git
Python 3.x
Go, Node etc. conforme os projetos
FFmpeg
Ollama
```

Opcional, mas recomendado posteriormente:

```text
Tailscale

GitHub CLI
jq
curl
make
```

## 24.6 Estrutura do projeto

```text
agent-system/
│
├── agents/
├── skills/
├── prompts/
├── workflows/
├── tools/
├── decision/
├── workers/
├── knowledge/
├── migrations/
├── infra/
│   ├── docker/
│   └── railway/
├── config/
│
├── docker-compose.yml
├── .env.example
├── .gitignore
└── README.md
```

## 24.7 Docker Compose

O `docker-compose.yml` deve conter os serviços necessários para a V1. Exemplo conceitual:

```yaml
services:
  hermes:
    ...

  litellm:
    ...

  decision:
    ...

  postgres:
    ...

  redis:
    ...

  langfuse:
    ...
```

Não é necessário fixar todos os detalhes imediatamente; o importante é que os serviços sejam isolados e reproduzíveis.

## 24.8 Variáveis de ambiente

Nunca colocar secrets no código ou no YAML versionado.

Exemplo:

```env
POSTGRES_URL=
REDIS_URL=
LITELLM_URL=
LANGFUSE_URL=
OPENROUTER_API_KEY=
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
MOONSHOT_API_KEY=
DEEPSEEK_API_KEY=
```

Localmente, usar `.env`.

No Railway, os mesmos nomes devem ser injetados como Variables/Secrets.

## 24.9 PostgreSQL e migrations

Não depender de cópia física do diretório de dados para migração.

Criar migrations versionadas:

```text
migrations/
├── 001_initial.sql
├── 002_memory.sql
├── 003_projects.sql
├── 004_agent_runs.sql
├── 005_cost_tracking.sql
└── ...
```

A aplicação deve ser capaz de criar um banco vazio apenas executando as migrations.

## 24.10 Backup desde o primeiro dia

Criar um fluxo simples:

```text
Postgres local
    ↓
pg_dump
    ↓
backup
    ↓
R2 / disco externo / backup local
```

A base local nunca deve ser o único local onde a memória existe.

## 24.11 Storage abstrato

Não usar caminhos absolutos:

```text
ERRADO
/home/usuario/projeto/document.pdf
```

Preferir referências abstratas:

```text
storage://documents/abc123.pdf
```

O backend pode apontar para filesystem local na V1 e para Railway Bucket/R2 depois.

## 24.12 Rede local

Dentro do Docker Compose, usar nomes de serviço:

```text
postgres
redis
litellm
langfuse
```

Não codificar `localhost` como dependência entre containers.

Exemplo:

```env
POSTGRES_HOST=postgres
REDIS_HOST=redis
LITELLM_HOST=litellm
```

Isso facilita a migração.

## 24.13 Ordem de subida

```text
1. Docker
2. PostgreSQL
3. Redis
4. LiteLLM
5. Decision Service / Jev
6. Langfuse
7. Hermes
8. Ollama
9. local workers
```

Healthchecks devem ser adicionados aos serviços importantes.

## 24.14 Ollama / modelos locais

Usar Ollama como runtime local para os primeiros experimentos.

Começar com:

```text
Qwen3 4B
Qwen3 8B
```

Quando houver 32 GB de RAM, testar modelos maiores quantizados.

O modelo local deve ser usado principalmente para:

- resumo;
- classificação;
- explicação de código;
- pequenas alterações;
- processamento privado;
- tarefas de baixo custo.

## 24.15 Whisper local

Instalar uma das opções:

```text
whisper.cpp
faster-whisper
```

Pipeline:

```text
audio/video
 ↓
FFmpeg
 ↓
Whisper
 ↓
transcript
 ↓
Qwen local ou LLM cloud
 ↓
summary / tasks / notes
```

Começar com qualidade/velocidade equilibradas e benchmarkar antes de escolher definitivamente o modelo.

## 24.16 Coding workspace local

O coding agent não deve ter acesso indiscriminado ao host.

Preferir:

```text
repo
 ↓
container/workspace
 ↓
agent
 ↓
tests
 ↓
patch/result
```

Ferramentas destrutivas devem possuir políticas explícitas.

## 24.17 Tailscale

Não expor Hermes, Redis, Postgres ou workers diretamente à Internet durante desenvolvimento.

Tailscale será útil futuramente para conectar:

```text
Railway / cloud
       ↕
Tailscale
       ↕
ThinkPad
```

Isso permite transformar o notebook em worker local mesmo depois da migração do control plane.

## 24.18 Testes da V1

Antes de qualquer migração, executar uma bateria de tarefas reais:

```text
10 tarefas simples
10 tarefas médias
10 debugging
5 refatorações
5 tarefas agentic
5 tarefas de pesquisa
5 tarefas financeiras
5 tarefas de resumo de áudio/vídeo
```

Registrar:

```text
modelo
input tokens
output tokens
iterations
tool calls
sucesso
tempo
custo
```

Esses dados serão a base do primeiro ajuste do Model Router.

---

# 25. Migração futura para Railway

A migração deve ser incremental.

## 25.1 O que será mantido

```text
agentes
skills
workflows
configuração lógica
migrations
Dockerfiles
prompts
routing policies
decision schemas
``

## 25.2 O que muda

```text
Docker Compose
      ↓
Railway Services
```

Serviços iniciais:

```text
Railway
├── Hermes
├── LiteLLM
├── Decision Service
├── PostgreSQL
├── Redis
└── Langfuse
```

## 25.3 Migração do banco

```text
local Postgres
      ↓
pg_dump
      ↓
Railway Postgres
      ↓
restore + migrations
```

## 25.4 Migração de arquivos

```text
local filesystem
      ↓
R2 / Railway Bucket
```

Como a aplicação usa referências abstratas, não será necessário reescrever o código de negócio.

## 25.5 Worker local permanece

Depois da migração:

```text
Railway = Control Plane

ThinkPad = Local Worker
```

O ThinkPad poderá continuar fornecendo:

- Whisper;
- Qwen local;
- FFmpeg;
- processamento privado;
- eventualmente GPU/CPU local;
- coding sandbox.

---

# 26. Arquitetura final

### V1 — desenvolvimento local

```text
                              USER
                               │
                               ▼
                          LOCAL MACHINE
                               │
      ┌────────────────────────┼────────────────────────┐
      ▼                        ▼                        ▼
   Hermes                 Decision/Jev             Knowledge
      │                        │                        │
      │                        ▼                        │
      │                  Model Router                  │
      │                        │                        │
      │            ┌───────────┼───────────┐           │
      │            ▼           ▼           ▼           │
      │          cheap       strong      frontier      │
      │            │           │           │           │
      └────────────┴───────────┼───────────┴───────────┘
                               ▼
                         Agent + Skills
                               │
                          Tools / Hooks
                               │
                          Validation
                               │
                          Escalation
                               │
                               ▼
                             RESULT

Local services:
├── PostgreSQL + pgvector
├── Redis
├── LiteLLM
├── Langfuse
├── Ollama
├── Whisper
└── FFmpeg
```

### V2 — control plane em Railway

```text
                         USER
                          │
                          ▼
                    RAILWAY CONTROL PLANE
                          │
        ┌─────────────────┼─────────────────┐
        ▼                 ▼                 ▼
     Hermes            Decision          Memory
     Gateway           Engine            Layer
        │                 │                 │
        └─────────────────┼─────────────────┘
                          ▼
                     Model Router
                          │
                   Agent + Skills
                          │
                     Tools/Hooks
                          │
                 ┌────────┴────────┐
                 ▼                 ▼
          Cloud Workers       Local Worker
                              ThinkPad
```

---

# 27. Ordem de implementação

## Fase 1 — ambiente local

```text
Docker
Docker Compose
PostgreSQL
Redis
Hermes
LiteLLM
Decision Service / Jev
Langfuse
Ollama
Whisper
FFmpeg
```

## Fase 2 — foundation dos agentes

```text
Engineering Agent
Skills
Hooks
Tools
Coding loops
Permissions
Budgets
```

## Fase 3 — knowledge

```text
pgvector
retrieval
Context Compiler
memory
events
projects
```

## Fase 4 — outros domínios

```text
Finance
Projects
Personal
Learning
```

## Fase 5 — automação

```text
Cron
Webhooks
Daily Review
Weekly Review
Notifications
```

## Fase 6 — otimização

```text
Model routing baseado em dados reais
Cost tracking
Cache tuning
Benchmark interno
Escalation tuning
```

## Fase 7 — cloud

```text
Railway Control Plane
Postgres migration
Redis migration
Worker local via Tailscale
Optional isolated cloud workers
```

---

```text
Knowledge Base
pgvector
Context Compiler
Memory
```

## Fase 4

```text
Finance
Projects
Personal
Learning
```

## Fase 5

```text
Cron
Webhooks
Daily workflows
Weekly workflows
```

## Fase 6

```text
Routing baseado em dados reais
Cost optimization
Caching
Worker isolation
```

## Fase 7

```text
IA local
Whisper
Qwen
Media pipeline
```

---

# 26. Critérios de sucesso

### Qualidade

- maioria das tarefas simples sem frontier;
- K2.7/DeepSeek resolvem grande parte das tarefas normais;
- K3 recebe tarefas realmente complexas;
- Sonnet/Opus são fallback, não padrão;
- modelos máximos são exceção;
- loops terminam corretamente.

### Economia

- alta proporção de operações determinísticas/cheap;
- decision layer barato;
- contexto enxuto;
- cache;
- token budgets;
- custo por task monitorado.

### Infraestrutura

- Railway como control plane;
- backups;
- secrets;
- private networking;
- workers isolados quando necessário.

### Segurança

- permissões por domínio;
- shell restrito;
- secrets protegidos;
- bloqueio de arquivos sensíveis;
- aprovação humana para ações críticas.

### IA local

- Whisper local;
- FFmpeg;
- Qwen3 4B;
- 32 GB RAM antes de testar modelos locais maiores;
- serviço `agent-edge`;
- política local vs cloud.

---

# 27. Resumo executivo

```text
1. Regra determinística tenta resolver.
2. Jev decide o que precisa acontecer.
3. O sistema recupera somente o contexto necessário.
4. O modelo mais barato capaz recebe a tarefa.
5. Hooks e ferramentas fazem o máximo possível sem LLM.
6. Testes validam.
7. Jev decide se termina, repara ou escala.
8. Só modelos fortes recebem tarefas que realmente justificam o custo.
9. Memória fica fora do LLM.
10. Railway mantém o control plane.
11. Workers externos entram somente quando houver necessidade real.
12. O ThinkPad funciona como worker local de mídia/IA privada.
```

A meta é atingir uma experiência próxima de agentes frontier por meio de **arquitetura superior de decisão, contexto, execução e escalonamento**, em vez de pagar por um modelo frontier em cada etapa.

---

# 28. Referências

- Hermes Agent: https://hermes-agent.nousresearch.com/docs/
- Hermes Hooks: https://hermes-agent.nousresearch.com/docs/user-guide/features/hooks/
- LiteLLM: https://docs.litellm.ai/
- Langfuse: https://langfuse.com/docs/
- Railway: https://docs.railway.com/
- OpenRouter: https://openrouter.ai/
- Whisper large-v3-turbo: https://huggingface.co/openai/whisper-large-v3-turbo
- Ollama Qwen3: https://ollama.com/library/qwen3
- Qwen3 4B Instruct: https://ollama.com/library/qwen3:4b-instruct
