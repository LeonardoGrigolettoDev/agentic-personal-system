# Sandbox (workers/sandbox): the SSH execution target of Hermes' terminal backend. Included by the root
# Makefile (-include mk/*.mk); uses its DC when defined.
DC ?= docker compose
SANDBOX_KEYS_DIR ?= data/sandbox/keys
SANDBOX_WORKSPACE_DIR ?= data/sandbox/workspace
SANDBOX_DC = $(DC) --profile agent

.PHONY: sandbox-keys sandbox-build sandbox-up sandbox-shell sandbox-ssh-check sandbox-test sandbox-verify-downloads

sandbox-keys: ## SSH keypair for Hermes -> sandbox (data/sandbox/keys, idempotent) + workspace dir
	@set -eu; umask 077; \
	dir="$(SANDBOX_KEYS_DIR)"; key="$$dir/id_ed25519"; mkdir -p "$$dir" "$(SANDBOX_WORKSPACE_DIR)"; \
	if [ ! -s "$$key" ]; then \
	  rm -f "$$key" "$$key.pub"; \
	  ssh-keygen -q -t ed25519 -N '' -C "aios-hermes@sandbox" -f "$$key"; \
	  echo "generated $$key"; \
	fi; \
	[ -s "$$key.pub" ] || ssh-keygen -y -f "$$key" > "$$key.pub"; \
	touch "$$dir/authorized_keys"; \
	grep -qxF -- "$$(cat "$$key.pub")" "$$dir/authorized_keys" || cat "$$key.pub" >> "$$dir/authorized_keys"; \
	chmod 700 "$$dir"; chmod 600 "$$key"; chmod 644 "$$key.pub" "$$dir/authorized_keys"; \
	[ "$$(id -u)" = 1000 ] || echo "warning: sandbox user 'agent' is uid 1000 but you are uid $$(id -u); $(SANDBOX_WORKSPACE_DIR) will be chowned to 1000" >&2; \
	echo "sandbox key: $$(ssh-keygen -lf "$$key.pub")"

sandbox-build: ## build the sandbox image (aios/sandbox:dev)
	$(SANDBOX_DC) build sandbox

sandbox-up: sandbox-keys ## start the sandbox and wait until sshd is healthy
	$(SANDBOX_DC) up -d --wait sandbox

sandbox-shell: ## interactive shell as 'agent' in /workspace (docker exec, bypasses sshd)
	$(SANDBOX_DC) exec -it -u agent -w /workspace sandbox env -u GITHUB_TOKEN bash -l

sandbox-ssh-check: ## exercise the real path: Hermes -> ssh agent@sandbox aios-check --json /workspace
	$(SANDBOX_DC) exec hermes ssh -i /opt/aios/ssh/id_ed25519 -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
	  agent@sandbox 'aios-check --json /workspace'

test: sandbox-test

sandbox-test: ## sandbox helper tests without Docker (temp git repos)
	bash workers/sandbox/tests/run.sh

sandbox-verify-downloads: ## re-download every pinned sandbox artifact and verify its sha256
	bash workers/sandbox/tests/verify-downloads.sh --full
