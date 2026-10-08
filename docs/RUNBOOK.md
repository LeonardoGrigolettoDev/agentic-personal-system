# RUNBOOK do AI Agent OS

Este é o manual de operação do repositório `agent-system`. A visão está em [ARCHITECTURE.md](ARCHITECTURE.md) e os contratos em [CONTRACTS.md](CONTRACTS.md). Aqui está o **como**:
- instalar e operar;
- fazer backup e restaurar;
- diagnosticar problemas;
- atualizar versões;
- observar o sistema;
- cuidar da segurança;
- ver onde cada seção da arquitetura está implementada;
- migrar para o Railway e voltar atrás.

O alvo é o ThinkPad (Linux Mint 22 / base Ubuntu noble, Ryzen 7 250, Radeon 780M, 16 GB).

> Convenções
> - `make <alvo>` roda na raiz do repo. `make help` lista os alvos, inclusive os de `mk/*.mk`.
> - Só o `bootstrap-host.sudo.sh` precisa de `sudo`.
> - Os scripts que escrevem fora do repo são **dry-run por padrão** e só escrevem com `--apply`: `railway-db-migrate.sh`, `storage-sync.sh` e `tailscale-edge.sh`.

## Mapa rápido

| Serviço | Onde (local) | Porta no host (só 127.0.0.1) | Profile compose |
|---|---|---|---|
| postgres (PG17 + pgvector) | container | 5432 | core |
| valkey | container | — | core |
| litellm | container | 4000 | core |
| decision (Go) | container | 8090 | core |
| knowledge | container | 8092 | core |
| hermes | container | 8642 | agent |
| sandbox (sshd) | container | — | agent |
| edge | container | 8093 | media |
| whisper | container | 8178 | media |
| Langfuse self-hosted | containers | 3000 | observability |
| Ollama | host (systemd, Vulkan) | 11434 | — |

Todo serviço HTTP expõe `GET /healthz` e `GET /readyz` sem autenticação. O resto exige `Authorization: Bearer <SERVIÇO>_API_KEY`.

---

## 1. Primeira instalação

### 1.1 Pré-requisitos

- O `ufw` precisa estar **ativo com `deny (incoming)`**: o bootstrap aborta se não estiver, porque o Ollama escuta em `0.0.0.0`.
  - `sudo ufw status verbose` mostra o estado.
  - Para ativar: `sudo ufw default deny incoming && sudo ufw enable`.
- Disco livre: cerca de 15 GB (imagens, modelos e volumes).
- O repo **não** pode ficar dentro de `~/Obsidian/*` nem de nenhuma pasta sincronizada: o `.env` e `backups/` guardam segredos.

### 1.2 Pacotes do sistema (único passo com sudo)

```bash
less infra/scripts/bootstrap-host.sudo.sh          # revise antes de rodar
sudo bash infra/scripts/bootstrap-host.sudo.sh     # INSTALL_TAILSCALE=1 para já instalar o Tailscale
```

O script instala:
- Docker Engine + Compose (repo deb822 com `Suites: noble`), com `daemon.json` (`"ip": "127.0.0.1"` e rotação de logs);
- ffmpeg, jq, gh, psql e sqlite3;
- o Ollama como serviço systemd, com override `OLLAMA_HOST=0.0.0.0`, contexto 4096, `KEEP_ALIVE=5m` e `MAX_LOADED_MODELS=1`;
- a regra do `ufw`: só a sub-rede do compose (`172.30.0.0/24`) chega à porta 11434.

**Depois, faça LOGOUT e LOGIN.** Os grupos `docker` e `render` só valem numa sessão nova.

### 1.3 Verificar o host

```bash
make doctor
```

Todo item precisa sair com ✔:

| Item com ✘ | Correção |
|---|---|
| `docker daemon not reachable` / `not in render group` | faltou o re-login |
| `ollama not answering` | `systemctl status ollama` e `journalctl -u ollama -e` |
| `vulkan GPU not detected` | veja §4.2 |

### 1.4 Segredos e chaves de provedores

```bash
make env          # cria .env (chmod 600) e gera todos os segredos internos; idempotente
$EDITOR .env      # cole ANTHROPIC / OPENROUTER / MOONSHOT / DEEPSEEK / OPENAI / TYPESAFE que tiver
```

- É preciso pelo menos **um provedor cloud com contexto ≥ 64K**, porque o Hermes recusa modelos menores. Sem nenhuma chave, `make litellm-config` avisa.
- **`LITELLM_SALT_KEY` nunca pode mudar** depois da primeira chave virtual criada. O backup copia o `.env` para `backups/<ts>/env.bak`.
- Opcionais:
  - `TELEGRAM_BOT_TOKEN` e `TELEGRAM_ALLOWED_USERS`: conversar com o chief e receber os crons;
  - `TELEGRAM_CHAT_ID` ou `NTFY_URL`: avisos de aprovação;
  - `LANGFUSE_*`: observabilidade, veja §6.

### 1.5 Modelos locais

```bash
make models whisper-model   # qwen3:4b-instruct-2507-q4_K_M (~2.5 GB) + qwen3-embedding:0.6b + whisper large-v3-turbo q5_0
```

### 1.6 Chaves do sandbox

```bash
make sandbox-keys   # data/sandbox/keys/{id_ed25519,id_ed25519.pub,authorized_keys}, idempotente
```

### 1.7 Subir o núcleo

```bash
make up-core
```

O alvo roda, nesta ordem:
1. `litellm-config`: gera `config/litellm/config.yaml` só com os provedores que têm chave;
2. postgres + valkey (espera o healthcheck);
3. `make migrate` (`migrations/NNN_*.sql`, cada uma numa transação);
4. litellm;
5. `litellm-keys.sh`: chaves virtuais com orçamento para hermes, decision, kb, edge e bench, gravadas no `.env`;
6. decision + knowledge.

### 1.8 Subir o agente

```bash
make up
```

O alvo:
1. confere `HERMES_LITELLM_KEY`;
2. sobe sandbox e hermes;
3. roda **`make hermes-setup`**, que aplica:
   - config do chief;
   - os perfis `engineering`, `finance`, `projects`, `personal` e `learning`;
   - o link do plugin `aios`;
   - os segredos que o próprio Hermes lê (`LITELLM_API_KEY`, `KNOWLEDGE_API_KEY`, e no chief `TELEGRAM_*` e
     Langfuse), copiados para o `.env` de cada perfil: o gateway multiplexa os perfis e, assim, só lê segredos do
     `.env` do perfil, nunca do ambiente do container (sem isso o LiteLLM recebe `no-key` e o MCP dá 401);
   - a `API_SERVER_KEY` de cada perfil (`HERMES_API_KEY_<PERFIL>`), para `/p/<perfil>/`;
   - os crons de revisão;
4. reinicia o Hermes.

Rode `make hermes-setup` de novo sempre que mudar `config/hermes/`, `agents/`, `workflows/` ou girar uma dessas
chaves. Ele é idempotente. Para recriar os crons a partir de `workflows/*.md`: `make hermes-setup ARGS=--recreate-cron`.

### 1.9 Teste de fumaça

```bash
make smoke          # health de tudo + chat local + dimensão do embedding (1024) + /v1/decide + modelos do Hermes
make hermes-doctor  # hermes doctor, config check e `hermes plugins doctor /opt/aios/plugins/aios --ci`
make sandbox-ssh-check
make stats          # RAM real por container; atualize a tabela de RAM do README se mudar muito
```

### 1.10 Rotina automática e extras

```bash
make timers-install     # systemd --user: backup às 03:00 e restart do Hermes às 04:00 (Persistent=true)
make edge-up            # opcional: worker de mídia (edge + whisper, profile media)
make restore-drill      # depois do primeiro backup: prova que ele restaura (veja §3.2)
```

---

## 2. Operação diária

### 2.1 Conversar com o Hermes

- **Terminal:** `make hermes-shell`, que roda `hermes chat` dentro do container.
- **Telegram:** com `TELEGRAM_BOT_TOKEN` e `TELEGRAM_ALLOWED_USERS` no `.env`, rode `make hermes-setup` e reinicie o Hermes.
- **API** (compatível com OpenAI; dá acesso total ao terminal, nunca exponha):

  ```bash
  source .env; curl -s localhost:8642/v1/models -H "Authorization: Bearer $HERMES_API_KEY" | jq
  ```

O plugin `aios` cuida de tudo sozinho em cada sessão:
- rota (`decision /v1/route`);
- contexto (`knowledge /v1/context/compile`);
- modelo por chamada (`/v1/models/resolve`);
- orçamento (`/v1/usage`);
- gate de reparo e escalonamento (`/v1/gate`).

### 2.2 Kanban: agentes de domínio

Os perfis de domínio rodam sob demanda como workers do Kanban. O chief cria as tarefas.

```bash
make hermes-kanban                                         # hermes kanban list
docker compose --profile agent exec hermes hermes kanban show <id>
docker compose --profile agent exec hermes hermes kanban create "Revisar PR 42" --assignee engineering --tenant nitro
docker compose --profile agent exec hermes hermes kanban tail <id>
```

O tenant da tarefa (`nitro`, `pessoal` ou `shared`) vira `HERMES_TENANT` no worker. O plugin bloqueia ferramentas de conhecimento de outro tenant.

### 2.3 Revisões agendadas (cron)

- Estão definidas em `config/hermes/profiles.yaml`, e os prompts ficam em `workflows/<nome>.md`. Quem roda é o gateway do chief.
- Horários (um tenant por execução): 07:30 morning-review (pessoal), 08:00 daily-planning (nitro, seg–sex), 09:00 engineering-review (nitro, seg–sex), 18:30 project-review (nitro, seg–sex), 20:00 learning-review (pessoal), 22:30 daily-reflection (pessoal), weekly-review aos domingos às 19:00 (pessoal) e weekly-review-nitro às sextas às 17:30.
- Nenhuma skill vai anexada ao job: cada workflow carrega a sua com `skill_view`, e a linha `Tenant desta execução` fica no topo do prompt, onde o plugin roteia.
- Depois de mudar um prompt em `workflows/`, recrie os jobs: `make hermes-setup ARGS=--recreate-cron`.

```bash
docker compose --profile agent exec hermes hermes cron list
docker compose --profile agent exec hermes hermes cron runs
docker compose --profile agent exec hermes hermes cron doctor     # falhas, entregas, next_run atrasado
docker compose --profile agent exec hermes hermes cron run <id>   # dispara no próximo tick
```

- A saída vai para `AIOS_CRON_DELIVER`: `local` por padrão, ou `telegram`.
- Se um job aparecer como `SKIPPED (missing …)` no `hermes-setup`, falta o `workflows/<nome>.md` correspondente.

### 2.4 Aprovações humanas

Uma aprovação nasce quando o gate quer escalar acima do `max_tier` do agente, ou quando o pedido é `tier7-fable`. Se `TELEGRAM_*` ou `NTFY_URL` estiverem configurados, chega um aviso.

```bash
make approvals                  # GET /v1/approvals?status=pending
make approve id=<uuid>          # POST /v1/approvals/<id> {"approve": true}
make approve id=<uuid> no=1     # rejeita
```

### 2.5 Relatórios de custo (§14, §18)

```bash
make report                     # r=costs (padrão): custo por tarefa concluída, por task_type × modelo
make report r=models            # também: agents, skills, loops, routes, escalations, spend
source .env; curl -s localhost:8090/v1/runs/<session_id> -H "Authorization: Bearer $DECISION_API_KEY" | jq
```

### 2.6 Base de conhecimento

```bash
make kb-ingest tenant=pessoal path=~/Obsidian/Pessoal              # idempotente (content_hash)
make kb-ingest tenant=nitro path=~/Obsidian/Nitro domain=engineering
make kb-search tenant=pessoal q="metas de estudo 2026"
make kb-stats
make kb-maintain                # expira memórias temporárias/substituídas, limpa chunks órfãos
make kb-project tenant=nitro slug=app-checkout name="App Checkout" repo=git@github.com:empresa/checkout.git
make kb-projects tenant=nitro   # projetos cadastrados (slug, status, repositório)
```

O cofre `Nitro` só entra no tenant `nitro`, e `Pessoal` só em `pessoal`: o `kb` recusa o contrário.

Cadastre os projetos que importam: memórias com `scope="project"` e `compile_context(project=…)` só funcionam com o
slug cadastrado, e a revisão de engenharia pega a URL do repositório daí. Enquanto um projeto não está cadastrado,
os agentes salvam com `scope="domain"` e o prefixo `[projeto <slug>]`.

### 2.7 Mídia

```bash
make transcribe f=knowledge/inbox/aula.m4a                         # whisper one-shot → .txt/.srt ao lado
make edge-pipeline f=~/Downloads/aula.m4a tenant=pessoal domain=learning title="Aula 3"
make edge-job id=<job_id>
make edge-maintain dry=1        # retenção: media/archive > MEDIA_RETENTION_DAYS (30)
```

### 2.8 Estado e logs

```bash
make ps
make health
make logs s=decision            # logs JSON
make stats
```

---

## 3. Backup e restauração

### 3.1 Backup

`make backup` roda `infra/scripts/backup.sh` e grava em `backups/<ts>/`:

| Arquivo | Conteúdo |
|---|---|
| `aios.dump`, `litellm.dump`, `langfuse.dump` | `pg_dump -Fc` de cada banco |
| `hermes-data.tgz` | `data/hermes` (`hermes_state.py export`): cada banco SQLite (`state.db`, `response_store.db`, perfis) é uma cópia consistente pela API de backup, com o Hermes rodando ou não; nunca um par `-wal`/`-shm` |
| `repo-config.tgz` | `config/`, `agents/`, `skills/`, `prompts/` e `workflows/` como estavam (o git também guarda) |
| `env.bak` | cópia do `.env`, modo 600 |

- O timer roda às 03:00 e o diretório guarda 14 dias.
- **Cópia fora da máquina (§24.10).** A base local nunca pode ser o único lugar onde a memória existe. Copie `backups/` para um disco externo ou para R2 / Railway Bucket:

  ```bash
  export STORAGE_S3_BUCKET=<bucket-de-backup> STORAGE_S3_ENDPOINT=... STORAGE_S3_ACCESS_KEY_ID=... STORAGE_S3_SECRET_ACCESS_KEY=...
  bash infra/scripts/storage-sync.sh --src backups --exclude env.bak            # dry-run
  bash infra/scripts/storage-sync.sh --src backups --exclude env.bak --apply
  ```

- O `env.bak` tem todos os segredos. Guarde-o no gerenciador de senhas, não em bucket.

### 3.2 Simulado de restauração (mensal, e antes de qualquer migração)

```bash
make restore-drill          # usa o backups/<ts> mais recente
KEEP=1 make restore-drill   # mantém o banco aios_restore_test para inspeção (DROP manual depois)
```

O simulado:
1. restaura `aios.dump` num banco descartável `aios_restore_test`, sempre `*_restore_test`, nunca o `aios`;
2. compara a contagem de linhas por tabela com o banco vivo (pode haver diferença por escritas posteriores ao backup);
3. confere que `schema_migrations`, o pgvector (operador `<->`), o TOC do `litellm.dump`, o `hermes-data.tgz` e o `env.bak` estão ok;
4. apaga o banco.

Qualquer `FAIL` significa que o backup **não serve**: investigue antes de seguir. Depois da migração, use `--db-only` para dumps baixados do bucket do Railway (veja §9.4).

### 3.3 Restauração real (desastre)

```bash
make down
docker compose up -d --wait postgres
ts=backups/<ts>
[ -f .env ] || install -m 600 $ts/env.bak .env      # só se o .env se perdeu (LITELLM_SALT_KEY!)
for db in aios litellm; do
  docker compose exec -T postgres psql -U postgres -c "DROP DATABASE IF EXISTS $db WITH (FORCE)" -c "CREATE DATABASE $db OWNER $db"
  docker compose exec -T postgres pg_restore -U postgres -d $db --exit-on-error --single-transaction < $ts/$db.dump
done
make migrate                                         # aplica migrations mais novas que o dump
mkdir -p data/hermes
python3 infra/railway/hermes/hermes_state.py import $ts/hermes-data.tgz data/hermes   # verifica, depois troca
make up && make smoke
```

O `import` verifica o arquivo inteiro antes de tocar no diretório e move o conteúdo antigo de `data/hermes` (inclusive
`-wal`/`-shm`, que o SQLite reaplicaria sobre o banco restaurado) para `data/hermes/.pre-import-<ts>-*/`. Apague esse
diretório depois do `make smoke`.

---

## 4. Diagnóstico

### 4.0 Bootstrap parado por pacote meio configurado (DKMS × kernel novo)

O bootstrap para logo no começo se o `dpkg` tem pacotes `iF`/`iU`. Caso visto em 2026-10-07: o upgrade do kernel
HWE para 7.0.0-34 tenta compilar o `evdi-dkms` 1.14.2 do Ubuntu (usado pela tela virtual do Sunshine), que não
compila no 7.0 (`DRM_ERROR`, `struct_mutex` saíram do kernel). Para ficar no 6.17, onde o evdi funciona:

```bash
apt-get -s remove --purge linux-image-7.0.0-34-generic linux-headers-7.0.0-34-generic \
  linux-generic-hwe-24.04 linux-headers-generic-hwe-24.04 linux-image-generic-hwe-24.04   # simula: só 5 remoções
sudo apt-get remove --purge linux-image-7.0.0-34-generic linux-headers-7.0.0-34-generic \
  linux-generic-hwe-24.04 linux-headers-generic-hwe-24.04 linux-image-generic-hwe-24.04
sudo dpkg --configure -a        # o evdi-dkms termina (compila só para os kernels com headers)
sudo apt-get -f install         # deve sair sem erro
dkms status                     # evdi ... 6.17.0-42-generic: installed
```

Remover só a imagem não basta: os metapacotes HWE puxariam o 7.0.0-38, com o mesmo erro. Sem eles, os kernels
novos param de chegar pela trilha HWE; a GA (`linux-image-generic`, 6.8) continua recebendo atualizações. Para
voltar ao 7.x, instale antes um evdi do upstream (`DisplayLink/evdi` ≥ 1.14.15 tem suporte preliminar ao 7.0)
e teste a tela virtual.

### 4.1 RAM e swap (16 GB)

- `make stats`, `free -h` e `ollama ps` mostram o uso.
- Cada container tem `mem_limit`: postgres 1g, litellm 1.5g, hermes 2g, sandbox 2g, edge 768m, decision 128m e knowledge 384m. Um OOM aparece como restart em `make ps` e como `OOMKilled` em `docker inspect <container> | jq '.[0].State'`.
- Para aliviar:
  - descarregue modelos (`ollama stop qwen3:4b-instruct-2507-q4_K_M`);
  - desligue a observabilidade (`make obs-down`) e a mídia (`docker compose --profile media stop`);
  - feche o Chrome.
- O Hermes cresce 30–100 MB/h (vazamento conhecido), por isso o timer o reinicia às 04:00.
- Com 32 GB: observabilidade sempre ligada, Qwen3 8B e `OLLAMA_MAX_LOADED_MODELS=2`.

### 4.2 Ollama na Radeon 780M (Vulkan)

- O ROCm não suporta a gfx1103. O backend é **Vulkan**, ligado por padrão.
- `vulkaninfo --summary` precisa mostrar RADV / 780M. Se não mostrar: `sudo apt install mesa-vulkan-drivers vulkan-tools`.
- A coluna `PROCESSOR` de `ollama ps` mostra a divisão GPU/CPU. Com 2 GiB de carve-out UMA, o modelo pode entrar só em parte na GPU. Aumentar o UMA na BIOS ajuda.
- Depois de atualizar o Ollama, **rode o bootstrap de novo**. Ele refaz o `setcap cap_perfmon`, que permite ao Vulkan ler a VRAM livre.
- Para forçar CPU: `sudo systemctl edit ollama` → `Environment="OLLAMA_VULKAN=0"`.
- Logs: `journalctl -u ollama -f`.

### 4.3 ufw e containers → Ollama

- Sintoma: `local-qwen`, `embed-local` e `decider-local` falham, e o resto funciona.
- `sudo ufw status verbose` precisa ter `11434/tcp ALLOW IN 172.30.0.0/24`.
- Teste de dentro de um container:

  ```bash
  docker compose exec litellm python3 -c "import urllib.request;print(urllib.request.urlopen('http://host.docker.internal:11434/api/version').read())"
  ```

- **Nunca** use `ufw allow 11434` sem origem, nem `ufw allow in on tailscale0`. O acesso pela tailnet passa pelo `tailscale serve` (§9).

### 4.4 LiteLLM: chaves e 401/403

| Sintoma | Causa provável | Correção |
|---|---|---|
| `401` em hermes, decision ou kb | chave virtual vazia ou errada no `.env`/container | `grep _LITELLM_KEY .env`. Se estiver vazia: `make litellm-keys` e recrie o serviço (`docker compose up -d decision knowledge`, `make up`) |
| `401`/`403` "key not allowed to access model" | modelo fora da allowlist da chave | veja `infra/scripts/litellm-keys.sh`. Para mudar: apague a variável no `.env`, rode `make litellm-keys` e recrie |
| `400`/budget exceeded | orçamento de 30 dias da chave esgotado | `curl -s "localhost:4000/key/info?key=$HERMES_LITELLM_KEY" -H "Authorization: Bearer $LITELLM_MASTER_KEY" \| jq` |
| erros de decrypt / credenciais ilegíveis | `LITELLM_SALT_KEY` mudou | recupere o valor original de `backups/*/env.bak` |
| modelo some do roteamento | provedor sem chave no `.env` | `make litellm-config && docker compose up -d litellm` |

Rode `source .env` antes dos `curl`. UI administrativa: `http://localhost:4000/ui`, com login pela master key.

### 4.5 Hermes

```bash
make hermes-doctor                     # doctor + config check + plugins doctor (aios) --ci
docker compose --profile agent exec hermes hermes plugins list
make logs s=hermes
```

- **Plugin `aios` sem efeito** (sem rota nem orçamento): confira `AIOS_DECISION_URL`/`DECISION_API_KEY` no container e `curl localhost:8090/readyz`. O plugin falha aberto quando a decision cai, mas registra em log.
- **"context length below 64000"**: o `context_length` está fixado em `config/hermes/config.yaml`. Rode `make hermes-setup`.
- **Config mexida à mão:** `make hermes-setup` reaplica só as chaves gerenciadas e guarda `config.yaml.aios-bak`.

### 4.6 Sandbox (SSH)

```bash
make sandbox-ssh-check                 # Hermes → ssh agent@sandbox aios-check --json /workspace
docker compose --profile agent logs sandbox   # logs JSON do entrypoint e do sshd
```

- **`Permission denied (publickey)`:** rode `make sandbox-keys` e depois `docker compose --profile agent restart sandbox hermes`.
- **`REMOTE HOST IDENTIFICATION HAS CHANGED`:** o volume `sandbox_hostkeys` foi recriado. Remova a entrada `sandbox` do `known_hosts` usado pelo Hermes, que fica em `/opt/data/.ssh/known_hosts` no container, porque o HOME do usuário hermes é `/opt/data`.
- **`GITHUB_TOKEN ignored`:** o token exige o tmpfs `/run/aios`, que está no serviço `sandbox` do `compose.yaml`.

### 4.7 Decision e knowledge

- `/readyz` 503 na decision quase sempre é migration faltando (`DECISION_REQUIRED_MIGRATION`). Rode `make migrate`.
- Knowledge com `embedding dim` errado: o modelo `embed-local` mudou e precisa reembeddar. Veja `workers/kb/README.md`.

---

## 5. Atualizações (bump de versões)

**Regra:** `make backup` → trocar o pin → subir → `make smoke` / `make hermes-doctor` → `make railway-test`.

`make railway-test` falha se uma imagem do kit do Railway divergir do compose ou do `.env.example`.

| Componente | Onde fica o pin | Notas |
|---|---|---|
| Hermes | `HERMES_TAG` em `.env.example` e `.env`, default em `compose.yaml`, `infra/railway/hermes/Dockerfile` (tag + digest) | leia as release notes; a config é migrada automaticamente, com backup. `docker compose --profile agent pull hermes && make up` |
| LiteLLM | `compose.yaml` (`ghcr.io/berriai/litellm:<tag>`) e `infra/railway/litellm/Dockerfile` | as migrations Prisma rodam no start, então faça backup antes |
| Postgres/pgvector | `compose.yaml` e `infra/railway/postgres/Dockerfile` | minor: troca direta. **Major (PG18) exige dump/restore**, e a partir do PG18 o volume muda para `/var/lib/postgresql` |
| Valkey | `compose.yaml` | |
| Langfuse | `LANGFUSE_TAG` | |
| whisper.cpp | `compose.yaml` (`main-vulkan`, tag móvel) | `docker compose --profile media pull whisper` |
| Ollama | instalador oficial (`curl -fsSL https://ollama.com/install.sh \| sh`) | rode o bootstrap de novo por causa do `setcap` |
| Go / decision | `decision/Dockerfile` e `infra/railway/decision/Dockerfile` (os dois estágios) | `make test` |
| knowledge / edge / bench (Python) | `workers/*/uv.lock`, `bench/uv.lock` | `uv lock --upgrade --project workers/kb` e depois `make test` |
| sandbox | ARGs com sha256 em `workers/sandbox/Dockerfile` | `make sandbox-verify-downloads` |
| Tailscale (edge-gw) | `infra/railway/edge-gw/Dockerfile` | |
| Cliente de backup | `infra/railway/db-backup/Dockerfile` | o `pg_dump` precisa ser ≥ major do servidor |

Digest de uma imagem: `docker buildx imagetools inspect <imagem>:<tag>`. Depois de um bump do Hermes, rode `make hermes-setup` e `make hermes-doctor`.

---

## 6. Observabilidade

| Opção | Custo | Quando usar |
|---|---|---|
| **Spend logs do LiteLLM + relatórios da decision** (padrão) | 0 MB a mais; fica no Postgres | sempre: custo por chave, modelo e dia; custo por tarefa concluída |
| **Langfuse Cloud** (Hobby) | 0 MB locais; prompts saem da máquina | traces por turno, LLM e ferramenta, sem RAM local |
| **Langfuse self-hosted** (`make obs-up`) | cerca de 2 GB de RAM (ClickHouse, MinIO, worker, web) | dados que não podem sair da máquina; com 32 GB |

### Spend logs e relatórios

```bash
make report r=spend
docker compose exec postgres psql -U postgres -d litellm -c \
  'SELECT model, round(sum(spend)::numeric, 4) AS usd, sum(total_tokens) FROM "LiteLLM_SpendLogs"
   WHERE "startTime" > now() - interval '\''1 day'\'' GROUP BY 1 ORDER BY 2 DESC'
```

A UI do LiteLLM fica em `http://localhost:4000/ui`.

### Langfuse Cloud

1. Crie um projeto em cloud.langfuse.com.
2. No `.env`, preencha `LANGFUSE_HOST=https://cloud.langfuse.com`, `LANGFUSE_PUBLIC_KEY` e `LANGFUSE_SECRET_KEY`.
3. Rode `make litellm-config && docker compose up -d litellm`. O render liga o callback `langfuse_otel`.
4. Rode `make hermes-setup`, que habilita o plugin `observability/langfuse` quando há chaves, e depois `docker compose --profile agent restart hermes`.

### Langfuse self-hosted

1. Descarregue os modelos do Ollama.
2. Rode `make obs-up` e abra `http://localhost:3000`. O login usa `LANGFUSE_ADMIN_EMAIL` e `LANGFUSE_ADMIN_PASSWORD`.
3. Aponte `LANGFUSE_HOST=http://langfuse-web:3000` e use as chaves `LANGFUSE_INIT_PROJECT_*` como `LANGFUSE_PUBLIC_KEY`/`SECRET_KEY`.
4. Para desligar: `make obs-down`.

### No Railway

- Os logs JSON saem com `railway logs -s <serviço>`. Use `--json` ou `-f '<filtro>'`.
- As métricas ficam no painel do serviço.
- Use o Langfuse Cloud. As shared variables `LANGFUSE_*` já estão referenciadas.

---

## 7. Checklist de segurança

- [ ] O `.env` tem modo `600`, `backups/` tem `700` e os dois estão no `.gitignore`. O repo não fica em pasta sincronizada (Obsidian, Drive).
- [ ] O `env.bak` e o `LITELLM_SALT_KEY` estão no gerenciador de senhas. Nenhuma cópia fora da máquina leva `env.bak` (`--exclude env.bak`).
- [ ] As portas publicadas aceitam só `127.0.0.1`, conforme o `ports:` do compose e o `daemon.json` (`"ip": "127.0.0.1"`). Confira com `ss -tlnp | grep -v 127.0.0`.
- [ ] O `ufw` está ativo com `deny incoming`. Só `172.30.0.0/24 → 11434`. Não há `ufw allow in on tailscale0`.
- [ ] A API do Hermes (8642) exige `API_SERVER_KEY` e nunca tem domínio público. Não há Funnel do Tailscale; `tailscale-edge.sh` aborta se houver.
- [ ] O Hermes, a decision e o kb usam **chaves virtuais** com orçamento e allowlist. A master key fica só no litellm. O `tier7-fable` só passa por aprovação (`make approvals`).
- [ ] Os perfis seguem `agents/*/agent.yaml`: `allowed_tools`, `deny_domains`, `allowed_tenants` e `max_tier`. O plugin bloqueia `.env`, `*.pem`, `id_*` e comandos destrutivos.
- [ ] O isolamento Nitro × Pessoal vale: o `kb` recusa cofre no tenant errado, o Hermes não monta `~/Obsidian` e o tenant do Kanban é `HERMES_TENANT`.
- [ ] O sandbox não tem `docker.sock` nem montagens do host além de `data/sandbox/*`, e roda com `cap_drop: ALL` e o mínimo para o sshd. O `GITHUB_TOKEN`, se usado, é fine-grained e restrito ao repo.
- [ ] O Telegram tem allowlist (`TELEGRAM_ALLOWED_USERS`), e o tópico ntfy é aleatório.
- [ ] Rotação de chave de serviço:
  1. troque no `.env` (ou apague para o `make env` gerar outra);
  2. recrie os serviços que a usam, com `docker compose up -d <svc>` / `make up`;
  3. rode `make hermes-setup`.

  No Railway, troque a shared variable e faça redeploy.
- [ ] No Railway: segredos só como shared variables (marque como *sealed*), nenhuma `variables.env` com valor literal (`make railway-check`) e nenhum domínio público. O TCP proxy do Postgres, se usado, é removido logo após a migração. A ACL da tailnet segue o mínimo: `tag:aios-cloud → tag:thinkpad:8093,11434`.

---

## 8. Checklist de fases: ARCHITECTURE.md seção a seção

**Legenda:** ✅ implementado · 🟡 parcial ou em componente paralelo · ⏳ futuro ou decisão pendente.

A ARCHITECTURE.md repete os números 26 e 27, que aparecem duas vezes cada. A tabela segue a ordem do arquivo.

| § | Tema | Status | Onde |
|---|---|---|---|
| 1 | Objetivo: custo por tarefa concluída | ✅ | métrica em `decision` `/v1/reports/costs`, `migrations/006_decision_ledger.sql`, `decision/internal/ledger/` |
| 2 | Arquitetura geral (intake → decision → retrieve → compiler → router → agent → hooks → gate) | ✅ | contratos em `docs/CONTRACTS.md` §0–§3. Fluxo em `tools/hermes-plugin/aios/` (`__init__.py`: route, context, resolve, usage, gate) |
| 3 | Hermes como runtime único | ✅ | `compose.yaml` (hermes), `config/hermes/config.yaml`, `infra/hermes/setup.py`, `tools/hermes-plugin/aios/` |
| 4 | Agentes por domínio | ✅ | `agents/{chief,engineering,finance,projects,personal,learning}/{agent.yaml,SOUL.md}`, `config/hermes/profiles.yaml` |
| 5 | Skills sob demanda | ✅ | `skills/<categoria>/<skill>/SKILL.md`, `skills/README.md`, `mk/skills.mk`, montadas em `/opt/shared-skills` |
| 6 | Decision Engine e Jev | ✅ | `decision/internal/{decide,rules,local,jev,openai}/`, `config/decision/rules.yaml`, `docs/research-jev-decisions.md` |
| 7 | Workflow de desenvolvimento (plan / tool / test-repair / reviewer / escalation) | ✅ | gate `decision/internal/policy/gate.go` (`/v1/gate`), hook `pre_verify` em `tools/hermes-plugin/aios/checks.py`, helpers `workers/sandbox/bin/aios-task-*` e `aios-check`; revisor via `auxiliary.review` em `config/hermes/config.yaml` |
| 8 | Hooks determinísticos | ✅ | `tools/hermes-plugin/aios/` (`pre_tool_call`: permissões, segredos, comandos destrutivos, tenant; `post_api_request`; `pre_verify`) |
| 9 | Context Compiler | ✅ | `workers/kb/src/kb/context.py`, `POST /v1/context/compile` |
| 10 | Knowledge Base (Postgres + pgvector + objetos + Redis) | ✅ | `migrations/004_knowledge.sql`, `workers/kb/src/kb/{ingest,retrieve,store,storage}.py`, valkey no compose |
| 11 | Memória (global, domínio, projeto, eventos, episódica) | ✅ | `workers/kb/src/kb/memory.py`, `migrations/002_tasks_runs_events.sql` e `004_knowledge.sql`, `skills/knowledge/memory_hygiene` |
| 12 | Permissões | ✅ | `agents/*/agent.yaml`, `migrations/003_permissions.sql`, `tools/hermes-plugin/aios/permissions.py` |
| 13 | Model routing (tiers 0–7) | ✅ | `config/routing.yaml`, `config/litellm/config.template.yaml`, `decision/internal/policy/route.go` |
| 14 | Cost per successful task | ✅ | `migrations/005_costs_decisions.sql` e `006_decision_ledger.sql`, `/v1/usage`, `/v1/reports/*` |
| 15 | Token budgets | ✅ | `token_budget`/`max_iterations`/`max_cost_per_run` em `agents/*/agent.yaml`; `/v1/budget/check` |
| 16 | Cache (prefixo estável) | ✅ | seção estática do plugin (`register_system_prompt_section`), cache Valkey da decision (`decision/internal/store/cache.go`), Redis no LiteLLM |
| 17 | Cron, eventos e manual | ✅ | crons em `config/hermes/profiles.yaml` + `infra/hermes/setup.py`, prompts em `workflows/*.md` (as 7 revisões + weekly-review-nitro); webhooks Hermes → `decision /v1/hermes-events`; pedido manual = sessão normal do chief |
| 18 | Observabilidade | ✅ | spend logs do LiteLLM, `/v1/reports/*`, Langfuse (profile `observability` ou Cloud); veja §6 |
| 19 | LiteLLM | ✅ | `compose.yaml`, `config/litellm/config.template.yaml`, `infra/scripts/{render-litellm-config.py,litellm-keys.sh}` |
| 20 | Hospedagem: Railway como control plane | ✅ preparado | `infra/railway/**`, `infra/scripts/railway-*.sh`, §9 deste runbook |
| 20 | Execution plane futuro (Hetzner, Daytona, Modal) | ⏳ | **decisão pendente**: não adicionar antes de haver necessidade real (§20, §27 fase 7) |
| 21 | Coding workers isolados | ✅ | `workers/sandbox/`, serviço `sandbox` + rede `sandbox_net` no `compose.yaml`, `mk/sandbox.mk`; no Railway, `infra/railway/sandbox/` |
| 22.1 | Transcrição | ✅ | whisper no compose (`make whisper-model`, `make transcribe`), edge `/transcribe` |
| 22.2 | Resumo local | ✅ | Ollama Qwen3 4B (`make models`), edge `/summarize` (`workers/edge/src/edge/summarize.py`) |
| 22.3 | Pipeline de áudio e vídeo | ✅ | `workers/edge/src/edge/pipeline.py`, `make edge-pipeline` |
| 22.4 | Diarização | 🟡 | opcional: `workers/edge/src/edge/diarize.py` (pyannote, `EDGE_EXTRAS=diarize`, `HF_TOKEN`). Sem o extra, retorna 501 |
| 22.5 | Worker local (`agent-edge`) via Tailscale | ✅ preparado | `workers/edge/`, `infra/scripts/tailscale-edge.sh`, `infra/railway/edge-gw/` |
| 23 | Armazenamento local e retenção | ✅ | `data/storage` (`storage://`), `workers/edge/src/edge/retention.py` (`MEDIA_RETENTION_DAYS`), `data/whisper-models` |
| 24.1–24.4 | Setup local, portabilidade, stack, hardware | ✅ | `compose.yaml`, `Makefile`, `README.md` (orçamento de RAM), `mem_limit`s; mapeamento de variáveis em `infra/railway/variables.md` |
| 24.5 | Pré-requisitos | ✅ | `infra/scripts/bootstrap-host.sudo.sh`, `make doctor` |
| 24.6–24.7 | Estrutura e Compose | ✅ | árvore do repo, `compose.yaml` (core, agent, media, bench, observability) |
| 24.8 | Variáveis de ambiente | ✅ | `.env.example`, `infra/scripts/gen-env.sh` |
| 24.9 | Migrations | ✅ | `migrations/NNN_*.sql`, `infra/scripts/migrate.sh` |
| 24.10 | Backup desde o primeiro dia | ✅ | `infra/scripts/backup.sh`, `infra/systemd/aios-backup.*`, `make restore-drill`; no Railway, `infra/railway/db-backup/` |
| 24.11 | Storage abstrato | ✅ | `workers/kb/src/kb/storage.py` (local/s3), `workers/edge/src/edge/storage.py`, `infra/scripts/storage-sync.sh` |
| 24.12 | Rede local por nome de serviço | ✅ | rede `aios` (172.30.0.0/24) + `sandbox_net` (172.30.1.0/24, só sandbox, hermes, edge e bench); nada usa `localhost` entre containers |
| 24.13 | Ordem de subida e healthchecks | ✅ | `make up-core` / `make up`, `depends_on: service_healthy` |
| 24.14 | Ollama | ✅ | override no bootstrap, `make models` |
| 24.15 | Whisper | ✅ | serviço `whisper` (Vulkan), `make transcribe` |
| 24.16 | Coding workspace local | ✅ | `workers/sandbox/` (SSH, `/workspace`, sem acesso ao host) |
| 24.17 | Tailscale | ✅ preparado | `infra/scripts/tailscale-edge.sh` (serve só na tailnet + ACL), `INSTALL_TAILSCALE=1` no bootstrap, `infra/railway/edge-gw/` |
| 24.18 | Testes da V1 (bateria de tarefas) | 🟡 | componente `bench/`: 55 tarefas, as contagens por categoria do §24.18 (CONTRACTS §7, `migrations/013_bench.sql`). Rode antes da migração (§9.1) |
| 25.1–25.2 | Migração para o Railway: o que fica e o que muda | ✅ preparado | `infra/railway/README.md`, `services.json`, `*/railway.json`, `variables.md` |
| 25.3 | Migração do banco | ✅ preparado | `infra/scripts/railway-db-migrate.sh` (testado ponta a ponta num cluster PG17 descartável) |
| 25.4 | Migração de arquivos | ✅ preparado | `infra/scripts/storage-sync.sh` |
| 25.5 | Worker local permanece | ✅ preparado | `edge-gw` + `tailscale-edge.sh` + `compose.override.yaml` do edge (§9.3) |
| 26 | Arquitetura final V1 / V2 | V1 ✅ · V2 ✅ preparado | V1 = compose; V2 = `infra/railway/`. "Cloud workers" ⏳ é decisão pendente |
| 27 | Ordem de implementação (fases 1–7) | ✅ 1–5 · 🟡 6 · ✅ 7 preparado | fase 6: roteamento aprendido (`learning` em `config/routing.yaml`) + `bench/`; fase 7: este kit (a execução depende do bench e da decisão de custo) |
| 26 (bis) | Critérios de sucesso | 🟡 | medidos por `make report` (loops, escalations, routes, costs), `make restore-drill` e o checklist de segurança (§7). A meta numérica só aparece com dados do `bench/` |
| 27 (bis) | Resumo executivo | ✅ | os 12 pontos são cobertos pelas linhas acima |
| 28 | Referências | ✅ | `docs/research-*.md` (versões verificadas em 2026-10-07) |

---

## 9. Migração para Railway (cutover) e rollback

O que fica no Railway e o que fica no ThinkPad:
- **Railway (control plane):** hermes, litellm, decision, knowledge, sandbox, postgres (PG17 + pgvector, a mesma imagem), redis, os buckets `aios-storage`/`aios-backups`, o cron `db-backup` e o gateway `edge-gw`.
- **ThinkPad (worker local):** Ollama, whisper e edge, vistos pela tailnet.

Detalhes do kit: [infra/railway/README.md](../infra/railway/README.md). Mapa de variáveis: [infra/railway/variables.md](../infra/railway/variables.md).

> ⚠️ O Railway descontinuou o Config as Code (`railway.json`):
> - serviços novos não podem mais usá-lo;
> - os arquivos deixam de ser lidos em **2026-12-01**.
>
> O caminho é o IaC: `make railway-iac` (repo do `origin` por padrão; ou `repo=<owner>/<nome>`) gera o `.railway/railway.ts` a partir dos mesmos arquivos. Os `railway.json` continuam como especificação revisada e testada. Em projeto antigo, também podem ser apontados como "Config File" até 2026-12-01.

### 9.1 Pré-requisitos (dias antes)

1. **Local verde:** `make smoke`, `make hermes-doctor`, `make restore-drill` e `make railway-test`.
2. **Bateria do `bench/` rodada (§24.18).** A decisão de migrar usa os números de custo dela.
3. **Contas:**
   - Railway em plano pago (`ALWAYS`, backups de volume);
   - Railway CLI recente (`railway --version`; o IaC exige uma CLI nova);
   - repo no GitHub (privado), com push feito;
   - conta Tailscale.
4. **`make railway-plan`:**
   - revise o **Drift**: valores do seu `.env` que diferem dos literais do Railway; ajuste `infra/railway/*/variables.env` se for intencional;
   - veja quais shared secrets o `.env` tem.
5. **Tailscale no ThinkPad:**

   ```bash
   INSTALL_TAILSCALE=1 sudo bash infra/scripts/bootstrap-host.sudo.sh   # ou: curl -fsSL https://tailscale.com/install.sh | sh
   bash infra/scripts/tailscale-edge.sh --acl        # cole no admin console → Access controls (tagOwners + grants + tests)
   sudo tailscale up --advertise-tags=tag:thinkpad
   sudo tailscale set --operator=$USER               # serve sem sudo
   make tailscale-edge-dry                           # confere login, tag, Funnel desligado; mostra o plano
   bash infra/scripts/tailscale-edge.sh --apply      # tailscale serve --tcp 8093 e 11434, só na tailnet
   ```

   No admin console, crie uma **auth key** com tag `tag:aios-cloud`, *pre-approved*, não reutilizável e não efêmera. Ela vai em `TS_AUTHKEY`.

### 9.2 Preparação no Railway (sem downtime; o local segue sendo a fonte da verdade)

1. **Projeto:**
   - `railway login`;
   - crie o projeto `aios` no painel ou com `railway init`;
   - `railway link` na raiz do repo.
2. **Shared variables** (Project Settings → Shared Variables → Raw Editor), **antes de criar os serviços**:

   ```bash
   make railway-plan | sed -n '/Paste secrets/,$p'      # mostra o grep exato; o valor vai direto para o clipboard
   base64 -w0 data/sandbox/keys/id_ed25519              # → AIOS_SANDBOX_SSH_KEY_B64
   cat data/sandbox/keys/id_ed25519.pub                 # → AIOS_SANDBOX_SSH_PUBKEY
   ssh-keygen -q -t ed25519 -N '' -C aios-sandbox-host -f /tmp/aios-hostkey && base64 -w0 /tmp/aios-hostkey; shred -u /tmp/aios-hostkey*
   #                                                     → AIOS_SANDBOX_HOST_KEY_B64
   ```

   - Também entram `TS_AUTHKEY`, `TZ`, `LANGFUSE_HOST` e **`AIOS_MAINTENANCE=1`**.
   - Deixe as `TELEGRAM_*` vazias até o passo 7 de §9.3: dois gateways no mesmo bot conflitam.
   - Marque os segredos como *sealed*.
   - O sandbox não sobe sem `AIOS_SANDBOX_HOST_KEY_B64` e `AIOS_SANDBOX_SSH_PUBKEY`. Sem uma host key fixa, cada redeploy quebraria o `known_hosts` do Hermes.
3. **Recursos.** Escolha uma das duas opções.
   - **IaC (recomendado):**

     ```bash
     make railway-iac > /tmp/railway.ts                            # repo = origin (ou repo=<owner>/<nome>)
     mkdir -p .railway && cp /tmp/railway.ts .railway/railway.ts   # sem segredos; pode ser versionado
     npm install --prefix .railway --no-save railway@3              # SDK do IaC em .railway/node_modules (não versione)
     railway config plan                                            # só leitura: revise cada linha
     railway config apply                                           # cria serviços, volumes, buckets, redis
     ```

   - **Painel:** crie cada serviço conforme `make railway-plan`:
     - fonte GitHub, Root Directory, variáveis coladas de `infra/railway/<svc>/variables.env`;
     - em projeto antigo, "Railway Config File" = `/infra/railway/<svc>/railway.json`;
     - o template Redis com o nome `redis`;
     - os buckets `aios-storage` e `aios-backups`;
     - os volumes.
4. **Até o passo 4 de §9.3, nada da nuvem escreve nos bancos.** Só rodam normalmente postgres, redis, edge-gw, sandbox, db-backup e os buckets.
   - **litellm** e **hermes** sobem em modo manutenção, por causa de `AIOS_MAINTENANCE=1`. O container fica no ar e responde ao health check, mas a aplicação não roda:
     - O LiteLLM, ao iniciar, aplica as migrations do Prisma e grava o hash da chave master no banco `litellm`. O `railway-db-migrate.sh` recusaria esse banco depois.
     - O gateway do Hermes não roda. Mesmo assim, o volume aceita `railway ssh` e `railway volume files`.
   - **decision** e **knowledge** conectam no `aios`. Tire-os do ar logo depois de criar os serviços:

     ```bash
     for s in decision knowledge; do railway down -s $s -y; done
     railway logs -s litellm -n 200 | grep -m1 AIOS_MAINTENANCE && railway logs -s hermes -n 200 | grep -m1 AIOS_MAINTENANCE
     ```

5. **Backups de volume:** ligue o agendamento (Daily/Weekly) em `pgdata`, `hermes-data` e `edge-gw-state`.
6. **Confira:**
   - `railway logs -s edge-gw` mostra `joined the tailnet`;
   - `tailscale status | grep aios-railway` no ThinkPad;
   - o initdb do postgres criou os papéis e os bancos:

     ```bash
     railway ssh -s postgres -- psql -X -U postgres -Atc "SELECT string_agg(datname, ',' ORDER BY datname) FROM pg_database"
     # aios,langfuse,litellm,postgres,template0,template1
     ```

     Se faltar algum banco, **pare**. O initdb só roda com o volume vazio, e um boot seguinte não o repete. Veja `railway logs -s postgres`. Nada foi copiado ainda, então recrie o volume `pgdata` e faça um novo deploy.

### 9.3 Janela de corte (cerca de 30 min de indisponibilidade)

1. **Último backup local:** `make backup && make restore-drill`.
2. **Pare quem escreve.** O postgres local fica de pé.

   ```bash
   docker compose --profile agent --profile media stop hermes sandbox edge
   docker compose stop decision knowledge litellm
   ```

3. **Abra o túnel para o Postgres do Railway** (outro terminal, fica aberto):

   ```bash
   railway connect postgres --tunnel-only -P 15432
   ```

   O CLI reconhece o banco pela imagem de origem, e este postgres é construído de um Dockerfile. Se a resposta for `No supported database found in service`, abra o mesmo túnel SSH direto:

   ```bash
   railway ssh config -s postgres          # grava o bloco "railway-postgres" em ~/.ssh/config
   ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:15432:127.0.0.1:5432 railway-postgres
   ```

   Sem nenhum túnel, use o Plano B: `railway-db-migrate.sh --dump-only` imprime, para cada banco, os comandos `railway volume files --volume pgdata upload` + `railway ssh … pg_restore`. **Não** abra o TCP proxy sem TLS.
4. **Banco:**

   ```bash
   read -rsp 'senha postgres do Railway (PG_SUPERUSER_PASSWORD): ' PGPW; echo
   export TARGET_DATABASE_URL="postgresql://postgres:${PGPW}@127.0.0.1:15432/postgres"
   make db-migrate-dry                               # pgvector, versões, bancos vazios, sessões abertas, plano
   bash infra/scripts/railway-db-migrate.sh --apply  # digite o host para confirmar
   ```

   O script:
   - grava os dumps em `backups/railway-migrate-<ts>/`, que ficam para o rollback;
   - restaura em transação única com os papéis donos;
   - **compara a contagem de linhas de cada tabela** e aplica as migrations pendentes.

   Ele recusa:
   - **sessões de outros clientes nos bancos de destino.** O dry run lista cada uma (banco, aplicação, IP). Volte ao passo 4 de §9.2.
   - **um `litellm` com tabelas.** Significa que o LiteLLM da nuvem subiu sem manutenção, e a mensagem de erro traz os passos para recriar o banco.

   Se aparecer `MISMATCH`, pare e volte para §9.5.
5. **Arquivos:**

   ```bash
   export STORAGE_S3_BUCKET=... STORAGE_S3_ENDPOINT=... STORAGE_S3_ACCESS_KEY_ID=... STORAGE_S3_SECRET_ACCESS_KEY=...   # aba Credentials do bucket aios-storage
   make storage-sync-dry && bash infra/scripts/storage-sync.sh --apply   # copy + check (size, one-way)
   ```

   O script usa URLs virtual-hosted (`FORCE_PATH_STYLE=false`), como os Railway Buckets. Se a aba Credentials disser "path", exporte `STORAGE_S3_FORCE_PATH_STYLE=true`.
6. **Estado do Hermes** (sessões, memórias, perfis, Kanban, cron):

   ```bash
   python3 infra/railway/hermes/hermes_state.py export data/hermes backups/hermes-cutover.tgz
   railway volume files --volume hermes-data upload backups/hermes-cutover.tgz /hermes-import.tgz
   railway ssh -s hermes -- chown hermes:hermes /opt/data/hermes-import.tgz   # o start.sh roda como hermes
   ```

   - **Export:** cada banco SQLite (o `state.db`, o `response_store.db` e os dos perfis) entra como cópia consistente, feita pela API de backup. Nenhum `-wal`/`-shm` entra no arquivo. O arquivo fica com modo 600, porque tem segredos do Hermes.
   - **Import:** o hermes da nuvem está em manutenção, então nada abre o volume agora. O `start.sh` importa `/opt/data/hermes-import.tgz` no próximo boot (passo 7), antes do `setup.py` e do gateway.
     - O conteúdo anterior do volume vai para `/opt/data/.pre-import-<ts>-*/`. Assim, nenhum `state.db-wal` de outra instância fica ao lado do banco importado.
     - Um arquivo corrompido para o boot sem tocar no volume.
   - O `config.yaml` local tem nomes do compose. Não tem problema: o `start.sh` reaplica o `setup.py` com a config já reescrita para `*.railway.internal`.
7. **Ligue a nuvem:**
   1. suba decision e knowledge:

      ```bash
      for s in decision knowledge; do railway redeploy -s $s --from-source -y; done
      ```

      O `railway down` removeu o deployment, e um `redeploy` simples recusa deployment removido. Se o seu CLI não tiver `--from-source`, use o painel: serviço → *Deploy latest commit*.
   2. preencha as shared `TELEGRAM_*` e, se quiser, troque `AIOS_CRON_DELIVER` para `telegram` no hermes;
   3. mude a shared `AIOS_MAINTENANCE` para `0` e aplique as mudanças:
      - o litellm e o hermes fazem redeploy;
      - o hermes importa o estado, roda o `setup.py` e sobe o gateway.
8. **Verificação** (pela tailnet; `aios-railway` = edge-gw):

   ```bash
   source .env
   curl -fsS http://aios-railway:4000/health/liveliness
   curl -fsS http://aios-railway:8090/readyz && curl -fsS http://aios-railway:8092/readyz
   curl -fsS http://aios-railway:8642/v1/models -H "Authorization: Bearer $HERMES_API_KEY" | jq -c '[.data[].id]'
   curl -fsS "http://aios-railway:8090/v1/approvals?status=pending" -H "Authorization: Bearer $DECISION_API_KEY" | jq length
   railway logs -s hermes -n 300 | grep -m1 'imported; the previous content'
   railway logs -s hermes -n 300 | grep -m1 'aios setup applied'
   railway ssh -s hermes -- hermes cron list
   railway ssh -s hermes -- hermes chat -q "Responda só: ok"          # rota → LiteLLM → provedor
   railway ssh -s litellm -- python3 -c "import urllib.request;print(urllib.request.urlopen('http://edge-gw.railway.internal:11434/api/version').read())"  # Ollama via tailnet
   ```

   Confira também os itens abaixo:
   - o sandbox: uma tarefa de código simples pelo chief deve rodar `aios-check`;
   - o pipeline de mídia (`transcript_to_notes`): o sandbox recebe `AIOS_EDGE_URL=http://edge-gw.railway.internal:8093`;
   - o Telegram;
   - na manhã seguinte, o log do `db-backup`.
9. **ThinkPad depois do corte:**
   1. crie `compose.override.yaml` para o edge (conteúdo em `infra/railway/variables.md`, usando `tailscale ip -4 aios-railway`);
   2. rode `docker compose --profile media up -d edge whisper`;
   3. desligue os timers locais: `systemctl --user disable --now aios-backup.timer aios-hermes-restart.timer`;
   4. **mantenha o volume do postgres local parado, mas intacto, por pelo menos 2 semanas**. Não rode `docker compose down -v`;
   5. com o estado conferido, apague `backups/hermes-cutover.tgz` (`shred -u`) e a cópia antiga do volume: `railway ssh -s hermes -- sh -c 'rm -rf /opt/data/.pre-import-*'`.

### 9.4 Depois do corte

- **Simulado com o backup da nuvem:**

  ```bash
  # aba Credentials do bucket aios-backups; Railway Buckets usam URLs virtual-hosted, e o provider "Other" do rclone
  # usaria path-style sem FORCE_PATH_STYLE=false
  export RCLONE_CONFIG_AIOS_TYPE=s3 RCLONE_CONFIG_AIOS_PROVIDER=Other RCLONE_CONFIG_AIOS_FORCE_PATH_STYLE=false \
    RCLONE_CONFIG_AIOS_ENDPOINT=... RCLONE_CONFIG_AIOS_REGION=... RCLONE_CONFIG_AIOS_ACCESS_KEY_ID=... RCLONE_CONFIG_AIOS_SECRET_ACCESS_KEY=...
  rclone copy aios:<bucket-aios-backups>/postgres/<ts>/ backups/cloud-<ts>/
  docker compose up -d --wait postgres
  bash infra/scripts/railway-restore-drill.sh --db-only backups/cloud-<ts>
  ```

- Feche o túnel. Se tiver criado um TCP proxy, remova-o.
- O `kb` CLI no host passa a usar o túnel e o LiteLLM da nuvem; veja `infra/railway/variables.md`.

### 9.5 Rollback

- **Antes do passo 7** (nada escreveu na nuvem):
  - rode `make up` no ThinkPad;
  - na nuvem, litellm e hermes continuam em manutenção;
  - apague o import pendente, para que ele não seja aplicado num boot futuro: `railway ssh -s hermes -- rm -f /opt/data/hermes-import.tgz`;
  - os serviços podem ficar como estão, ou sair com `railway down -s <svc> -y`.
- **Depois do passo 7** (a nuvem recebeu escritas e precisa voltar):
  1. Pare quem escreve na nuvem, **mantendo o container do Hermes vivo**. O export do passo 4 precisa de `railway ssh` e `railway volume files`, e os dois exigem um deployment ativo.
     - Mude a shared `AIOS_MAINTENANCE` para `1` e aplique. O litellm e o hermes fazem redeploy em manutenção, e o gateway recebe SIGTERM e fecha os bancos.
     - Tire o resto do ar e espere a manutenção:

       ```bash
       for s in decision knowledge; do railway down -s $s -y; done
       railway logs -s hermes -n 200 | grep -m1 AIOS_MAINTENANCE
       ```

  2. Faça o dump da nuvem pelo túnel (o mesmo do passo 3 de §9.3):

     ```bash
     railway connect postgres --tunnel-only -P 15432 &        # ou o ssh -L de §9.3; ou o dump mais recente do bucket aios-backups
     mkdir -p backups/rollback && read -rsp 'senha: ' PGPASSWORD; export PGPASSWORD
     for db in aios litellm; do
       docker run --rm --network host -e PGPASSWORD pgvector/pgvector:0.8.7-pg17-trixie \
         pg_dump -h 127.0.0.1 -p 15432 -U postgres -Fc $db > backups/rollback/$db.dump
     done
     ```

  3. Restaure localmente com os passos de §3.3 (DROP/CREATE + `pg_restore` de `backups/rollback/*.dump`), depois `make migrate`.
  4. Traga o estado do Hermes de volta:

     ```bash
     railway ssh -s hermes -- /opt/hermes/.venv/bin/python /opt/aios/railway/hermes_state.py export /opt/data /opt/data/hermes-export.tgz
     railway volume files --volume hermes-data download /hermes-export.tgz backups/rollback/hermes-data.tgz
     railway ssh -s hermes -- rm -f /opt/data/hermes-export.tgz
     python3 infra/railway/hermes/hermes_state.py import backups/rollback/hermes-data.tgz data/hermes
     sudo chown -R 1000:1000 data/hermes   # ou: docker run --rm -v $PWD/data/hermes:/d alpine chown -R 1000:1000 /d
     ```

     - **Export:** copia cada SQLite pela API de backup, inclusive o que ainda estava no `-wal`. Rodando como root via `railway ssh`, ele assume o dono do volume, então nenhum arquivo de root fica para trás.
     - **Import:** move o conteúdo local anterior para `data/hermes/.pre-import-<ts>-*/` antes de extrair. Nenhum `-wal`/`-shm` antigo fica ao lado do banco restaurado.
  5. Traga os arquivos: `bash infra/scripts/storage-sync.sh --pull --apply`.
  6. Religue o local:
     1. remova o `compose.override.yaml` do edge;
     2. rode `make up && make smoke`;
     3. rode `make timers-install`.

     O `tailscale serve` pode continuar ligado.
  7. Só então tire a nuvem do ar: `for s in hermes litellm sandbox; do railway down -s $s -y; done`.
  8. Registre a causa em `docs/` antes de tentar de novo. Os serviços do Railway ficam parados, sem deployment ativo, até lá.
