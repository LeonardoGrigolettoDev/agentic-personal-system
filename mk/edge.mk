# agent-edge media worker (workers/edge). Included by the root Makefile (-include mk/*.mk).
DC ?= docker compose
UV ?= $(HOME)/.local/bin/uv
EDGE_URL ?= http://127.0.0.1:8093
EDGE_AUTH = -H "Authorization: Bearer $$EDGE_API_KEY"

.PHONY: edge-up edge-logs edge-health edge-test edge-lock edge-dev edge-maintain edge-upload edge-pipeline \
        edge-job edge-jobs

# `make test` also runs the edge suite (extra prerequisite; the root recipe is unchanged)
test: edge-test

edge-up: ## start edge + whisper (profile media)
	@mkdir -p data/storage
	$(DC) --profile media up -d --build --wait edge

edge-logs: ## follow edge logs
	$(DC) --profile media logs -f --tail=100 edge

edge-health: ## edge readiness (storage, ffmpeg, whisper; litellm/knowledge informative)
	curl -sS $(EDGE_URL)/readyz | jq

edge-test: ## edge unit tests (offline; live-service tests skip when unreachable)
	$(UV) run --project workers/edge pytest -q workers/edge/tests

edge-lock: ## refresh workers/edge/uv.lock after editing its pyproject.toml
	cd workers/edge && $(UV) lock

edge-dev: ## run edge on the host on :8093 (dev; needs ffmpeg on the host)
	@set -a; source .env; set +a; mkdir -p data/storage; \
	STORAGE_LOCAL_ROOT=$(CURDIR)/data/storage WHISPER_URL=http://127.0.0.1:8178 LITELLM_BASE_URL=http://127.0.0.1:4000 \
	KNOWLEDGE_URL=http://127.0.0.1:8092 $(UV) run --project workers/edge edge serve --host 127.0.0.1 --port 8093

edge-maintain: ## media retention (archive > MEDIA_RETENTION_DAYS, stale processing): make edge-maintain [dry=1]
	$(DC) --profile media exec edge edge maintain $(if $(dry),--dry-run)

edge-upload: ## upload media: make edge-upload f=~/Downloads/aula.m4a
	@[ -n "$(f)" ] || { echo "usage: make edge-upload f=<audio|video file>"; exit 1; }
	@source .env; curl -fsS $(EDGE_URL)/upload $(EDGE_AUTH) -F "file=@$(f)" | jq

edge-pipeline: ## upload + transcribe + summarize + ingest (async): make edge-pipeline f=aula.m4a tenant=pessoal [domain=learning] [title="Aula 3"]
	@[ -n "$(f)" ] && [ -n "$(tenant)" ] || { echo "usage: make edge-pipeline f=<file> tenant=nitro|pessoal|shared [domain=<agent>] [title=...]"; exit 1; }
	@source .env; uri=$$(curl -fsS $(EDGE_URL)/upload $(EDGE_AUTH) -F "file=@$(f)" | jq -r .uri); echo "uploaded -> $$uri"; \
	jq -n --arg s "$$uri" --arg t "$(tenant)" --arg d "$(domain)" --arg ti "$(title)" \
	  '{source: $$s, tenant: $$t, async: true} + (if $$d != "" then {domain: $$d} else {} end) + (if $$ti != "" then {title: $$ti} else {} end)' \
	| curl -fsS $(EDGE_URL)/pipeline $(EDGE_AUTH) -H 'Content-Type: application/json' -d @- | jq

edge-job: ## job status/result: make edge-job id=<job_id>
	@[ -n "$(id)" ] || { echo "usage: make edge-job id=<job_id>"; exit 1; }
	@source .env; curl -fsS $(EDGE_URL)/jobs/$(id) $(EDGE_AUTH) | jq

edge-jobs: ## recent edge jobs (in-memory, since the last restart)
	@source .env; curl -fsS $(EDGE_URL)/jobs $(EDGE_AUTH) | jq
