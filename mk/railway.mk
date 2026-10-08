# Railway control plane (ARCHITECTURE §20, §25) + tailnet edge (§22.5, §24.17) + restore drill (§24.10).
# Included by the root Makefile (-include mk/*.mk). Every target here is offline, read-only or a dry run,
# except restore-drill, which only creates and drops the scratch database aios_restore_test.
# The writing steps are run by hand from docs/RUNBOOK.md ("Migração para Railway").
.PHONY: railway-plan railway-check railway-iac db-migrate-dry storage-sync-dry tailscale-edge-dry restore-drill railway-test

railway-plan: ## Railway services/variables/shared vars from infra/railway/* (offline, no API calls)
	bash infra/scripts/railway-plan.sh

railway-check: ## validate infra/railway/* (references, secrets, Dockerfiles)
	bash infra/scripts/railway-plan.sh --check

railway-iac: ## print .railway/railway.ts (Railway IaC): make railway-iac [repo=<owner>/<name>]
	bash infra/scripts/railway-plan.sh --iac $(if $(repo),--repo $(repo))

db-migrate-dry: ## preflight local -> Railway Postgres copy, read-only: TARGET_DATABASE_URL=... make db-migrate-dry
	bash infra/scripts/railway-db-migrate.sh $(if $(dbs),--dbs $(dbs))

storage-sync-dry: ## rclone dry run data/storage -> bucket (STORAGE_S3_* or RCLONE_CONFIG_AIOS_* in the env)
	bash infra/scripts/storage-sync.sh

tailscale-edge-dry: ## plan tailnet-only serve of edge :8093 + Ollama :11434 and print the ACL policy
	bash infra/scripts/tailscale-edge.sh

restore-drill: ## restore the newest backups/<ts>/aios.dump into aios_restore_test, sanity checks, drop it
	bash infra/scripts/railway-restore-drill.sh

test: railway-test

railway-test: ## tests for the Railway kit (dry runs with PATH shims, railway.json schema, compose parity)
	bash infra/railway/tests/run.sh
