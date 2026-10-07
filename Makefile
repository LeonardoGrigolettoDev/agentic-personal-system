SHELL := /bin/bash
.SHELLFLAGS := -eo pipefail -c
DC := docker compose
UV := $(HOME)/.local/bin/uv
GO := $(HOME)/.local/bin/go
KB := $(UV) run --project workers/kb kb
OLLAMA_CHAT := qwen3:4b-instruct-2507-q4_K_M
OLLAMA_EMBED := qwen3-embedding:0.6b
WHISPER_MODEL := large-v3-turbo-q5_0

.DEFAULT_GOAL := help
.PHONY: help doctor env litellm-config models whisper-model up-core up down ps logs migrate psql litellm-keys \
        hermes-config hermes-shell hermes-doctor obs-up obs-down transcribe health smoke backup stats \
        timers-install test kb-ingest kb-search kb-stats kb-maintain

help: ## list targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

doctor: ## check host prerequisites (after the sudo bootstrap + re-login)
	@ok(){ printf "  \033[32m✔\033[0m %s\n" "$$1"; }; bad(){ printf "  \033[31m✘\033[0m %s\n" "$$1"; }; \
	for t in docker ffmpeg jq gh psql ollama; do command -v $$t >/dev/null && ok "$$t" || bad "$$t missing (run sudo bootstrap)"; done; \
	[ -x $(UV) ] && ok "uv" || bad "uv missing"; [ -x $(GO) ] && ok "go" || bad "go missing"; \
	docker compose version >/dev/null 2>&1 && ok "docker compose" || bad "docker compose not usable (re-login for docker group?)"; \
	docker info >/dev/null 2>&1 && ok "docker daemon reachable" || bad "docker daemon not reachable as $$USER"; \
	id -nG | grep -qw render && ok "in render group" || bad "not in render group (re-login)"; \
	curl -fsS localhost:11434/api/version >/dev/null 2>&1 && ok "ollama $$(curl -s localhost:11434/api/version | jq -r .version)" || bad "ollama not answering on :11434"; \
	vulkaninfo --summary 2>/dev/null | grep -q -i 'radv\|780M\|phoenix' && ok "vulkan GPU visible" || bad "vulkan GPU not detected"; \
	free -m | awk '/Mem:/{printf "  ℹ RAM available: %d MiB\n", $$7}'

env: ## create .env and generate secrets
	bash infra/scripts/gen-env.sh

litellm-config: ## render config/litellm/config.yaml with only the providers that have keys
	$(UV) run --quiet --with pyyaml python infra/scripts/render-litellm-config.py

models: ## pull local Ollama models (chat 2.5 GB + embeddings 0.6 GB)
	ollama pull $(OLLAMA_CHAT)
	ollama pull $(OLLAMA_EMBED)

whisper-model: ## download the whisper.cpp model (large-v3-turbo q5_0, ~550 MB)
	mkdir -p data/whisper-models
	[ -f data/whisper-models/ggml-$(WHISPER_MODEL).bin ] || docker run --rm --user $$(id -u):$$(id -g) \
	  -v "$$PWD/data/whisper-models:/models" ghcr.io/ggml-org/whisper.cpp:main-vulkan \
	  download-ggml-model.sh $(WHISPER_MODEL) /models

up-core: litellm-config ## start core in dependency order: db -> migrate -> litellm -> keys -> decision + knowledge
	mkdir -p data/storage
	$(DC) config -q
	$(DC) up -d --wait postgres valkey
	$(MAKE) --no-print-directory migrate
	$(DC) up -d --wait litellm
	bash infra/scripts/litellm-keys.sh
	$(DC) up -d --build --wait decision knowledge

up: up-core hermes-config ## core + Hermes agent
	@source .env; [ -n "$$HERMES_LITELLM_KEY" ] || { echo "HERMES_LITELLM_KEY empty - run make litellm-keys"; exit 1; }
	$(DC) --profile agent up -d --wait hermes

down: ## stop everything (volumes kept)
	$(DC) --profile agent --profile observability --profile media down

ps: ## container status
	$(DC) --profile agent --profile observability --profile media ps

logs: ## follow logs: make logs s=litellm
	$(DC) --profile agent --profile observability logs -f --tail=100 $(s)

migrate: ## apply SQL migrations
	bash infra/scripts/migrate.sh

psql: ## psql into the aios DB
	$(DC) exec postgres psql -U aios -d aios

litellm-keys: ## create budgeted virtual keys (hermes/decision/kb) into .env
	bash infra/scripts/litellm-keys.sh

hermes-config: ## seed data/hermes (config.yaml, SOUL.md, .env) - never overwrites Hermes-owned files
	@mkdir -p data/hermes data/whisper-models data/storage backups knowledge/inbox knowledge/processed
	@[ -f data/hermes/config.yaml ] || cp config/hermes/config.yaml data/hermes/config.yaml
	@[ -f data/hermes/SOUL.md ] || cp agents/chief/SOUL.md data/hermes/SOUL.md
	@source .env; umask 077; printf 'LITELLM_API_KEY=%s\nAPI_SERVER_KEY=%s\n' "$$HERMES_LITELLM_KEY" "$$HERMES_API_KEY" > data/hermes/.env
	@echo "data/hermes seeded"

hermes-shell: ## interactive Hermes chat inside the container
	$(DC) --profile agent exec -it hermes hermes chat

hermes-doctor: ## hermes doctor + config check
	$(DC) --profile agent exec hermes hermes doctor
	$(DC) --profile agent exec hermes hermes config check

obs-up: ## start self-hosted Langfuse v4 (~2 GB RAM; stop Ollama models first)
	$(DC) --profile observability up -d --wait

obs-down: ## stop self-hosted Langfuse
	$(DC) --profile observability stop langfuse-web langfuse-worker clickhouse minio

transcribe: ## transcribe pt-BR audio/video: make transcribe f=knowledge/inbox/aula.m4a
	@[ -n "$(f)" ] || { echo "usage: make transcribe f=knowledge/inbox/<file>"; exit 1; }
	$(DC) --profile media run --rm --no-deps --entrypoint sh whisper -c '\
	  set -e; in="/audio/$(notdir $(f))"; base="/audio/$(basename $(notdir $(f)))"; \
	  ffmpeg -nostdin -loglevel error -y -i "$$in" -ar 16000 -ac 1 -c:a pcm_s16le /tmp/in.wav; \
	  whisper-cli -m /models/ggml-$(WHISPER_MODEL).bin -l pt -f /tmp/in.wav -otxt -osrt -of "$$base"'
	@echo "-> knowledge/inbox/$(basename $(notdir $(f))).txt / .srt"

health: ## health endpoints
	curl -fsS localhost:4000/health/liveliness && echo
	curl -fsS localhost:8090/healthz && echo
	curl -fsS localhost:8090/readyz && echo
	curl -fsS localhost:8092/readyz && echo
	-curl -fsS localhost:8642/health && echo

smoke: health ## end-to-end smoke: local chat, embeddings, decision, hermes
	@source .env; echo "== local chat"; curl -fsS localhost:4000/v1/chat/completions -H "Authorization: Bearer $$KB_LITELLM_KEY" \
	  -H 'Content-Type: application/json' -d '{"model":"local-qwen","messages":[{"role":"user","content":"Responda só: ok"}]}' | jq -r '.choices[0].message.content'
	@source .env; echo "== embeddings dim"; curl -fsS localhost:4000/v1/embeddings -H "Authorization: Bearer $$KB_LITELLM_KEY" \
	  -H 'Content-Type: application/json' -d '{"model":"embed-local","input":"olá mundo"}' | jq '.data[0].embedding|length'
	@source .env; echo "== decision"; curl -fsS localhost:8090/v1/decide -H "Authorization: Bearer $$DECISION_API_KEY" \
	  -H 'Content-Type: application/json' -d @decision/testdata/route.json | jq '.answers'
	@source .env; echo "== hermes models"; curl -fsS localhost:8642/v1/models -H "Authorization: Bearer $$HERMES_API_KEY" | jq -c '[.data[].id]' || true

backup: ## pg_dump + hermes state -> backups/<ts>
	bash infra/scripts/backup.sh

stats: ## container RAM/CPU + host free memory
	docker stats --no-stream --format 'table {{.Name}}\t{{.MemUsage}}\t{{.CPUPerc}}'
	@free -h | head -2
	-@ollama ps

timers-install: ## install systemd --user timers (nightly backup 03:00, hermes restart 04:00)
	mkdir -p ~/.config/systemd/user
	cp infra/systemd/aios-*.{service,timer} ~/.config/systemd/user/
	systemctl --user daemon-reload
	systemctl --user enable --now aios-backup.timer aios-hermes-restart.timer
	systemctl --user list-timers 'aios-*'

test: ## unit tests (Go decision service + Python KB worker)
	cd decision && $(GO) vet ./... && $(GO) test ./...
	$(UV) run --project workers/kb pytest -q workers/kb/tests

kb-ingest: ## ingest into the KB: make kb-ingest tenant=pessoal path=~/Obsidian/Pessoal [domain=learning]
	@[ -n "$(tenant)" ] && [ -n "$(path)" ] || { echo "usage: make kb-ingest tenant=nitro|pessoal|shared path=<dir|file> [domain=...]"; exit 1; }
	$(KB) ingest --tenant $(tenant) $(if $(domain),--domain $(domain)) "$(path)"

kb-search: ## hybrid search: make kb-search tenant=pessoal q="..."
	$(KB) search --tenant $(tenant) "$(q)"

kb-stats: ## KB document/chunk counts per tenant/domain
	$(KB) stats

kb-maintain: ## expire temporary/superseded memories, drop orphan chunks
	$(KB) maintain
