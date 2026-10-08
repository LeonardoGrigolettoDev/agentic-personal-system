# Hermes skills + scheduled review prompts (skills/README.md). Included by the root Makefile.
.PHONY: skills-test skills-validate skills-memory-deprecate

KNOWLEDGE_URL ?= http://127.0.0.1:8092

skills-test: ## skills: frontmatter/layout + script tests (HERMES_PYTHON=<hermes venv python> adds Hermes' own checks)
	PYTHONDONTWRITEBYTECODE=1 $(UV) run --quiet --with pytest --with pyyaml pytest -q -p no:cacheprovider skills/tests

test: skills-test

skills-validate: ## skills: validate with the Hermes container (linter, scanner, discovery, cron prompt scan)
	$(DC) --profile agent exec -T hermes $(HERMES_PY) /opt/shared-skills/tests/hermes_validate.py \
	  /opt/shared-skills --workflows /opt/aios/workflows

skills-memory-deprecate: ## skills: after memory_hygiene: make skills-memory-deprecate tenant=pessoal ids="<uuid> ..."
	@case "$(tenant)" in nitro|pessoal|shared) ;; *) echo 'usage: make skills-memory-deprecate tenant=nitro|pessoal|shared ids="<uuid> ..."'; exit 1 ;; esac
	@[ -n "$(ids)" ] || { echo 'ids="<uuid> ..." is required'; exit 1; }
	@source .env; for id in $(ids); do \
	  case "$$id" in *[!0-9a-f-]*|"") echo "invalid memory id: $$id"; exit 1 ;; esac; \
	  printf 'Authorization: Bearer %s\n' "$$KNOWLEDGE_API_KEY" | curl -fsS -X POST -H @- \
	    "$(KNOWLEDGE_URL)/v1/memories/$$id/deprecate?tenant=$(tenant)" >/dev/null && echo "deprecated $$id"; \
	done
