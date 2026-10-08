# Bench (bench/): the V1 test battery (docs/CONTRACTS.md §7). Included by the root Makefile (-include mk/*.mk);
# uses its DC/UV when defined. Selection variables: category=, task= (both repeatable, space separated),
# repeat=N, model=ALIAS (refused while DECISION_API_KEY is set: routing picks the model), since=7d.
DC ?= docker compose
UV ?= $(HOME)/.local/bin/uv
BENCH_CLI = $(UV) run --project bench bench
BENCH_DC = $(DC) --profile agent --profile bench
BENCH_ARGS = $(foreach c,$(category),--category $(c)) $(foreach t,$(task),--task $(t)) \
             $(if $(repeat),--repeat $(repeat)) $(if $(model),--model $(model))

.PHONY: bench-validate bench-test bench-fixtures bench-list bench-dry-run bench-build bench-run bench-report

bench-validate: ## lint bench tasks: JSON schema, fixtures, routing/agent refs, 10/10/10/5/5/5/5/5 counts
	$(BENCH_CLI) validate

test: bench-test

bench-test: ## bench tests (offline; + Postgres round trip when BENCH_TEST_DATABASE_URL is set)
	$(UV) run --project bench pytest -q bench/tests

bench-fixtures: ## generate media fixtures (tone always; speech/video need espeak-ng/ffmpeg, optional)
	$(BENCH_CLI) fixtures

bench-list: ## list bench tasks: make bench-list [category=media]
	$(BENCH_CLI) list $(foreach c,$(category),--category $(c))

bench-dry-run: ## what bench-run would submit (no calls): make bench-dry-run [category=..] [task=..] [repeat=N] [model=..]
	$(BENCH_CLI) run --dry-run $(BENCH_ARGS)

bench-build: ## build the bench image (aios/bench:dev)
	$(BENCH_DC) build bench

bench-run: ## run the battery from the bench container: make bench-run [category=..] [task=..] [repeat=N] [model=..]
	$(BENCH_DC) run --rm bench run $(BENCH_ARGS)

bench-report: ## success, cost per success, router accuracy + suggested routing.yaml diff: make bench-report [since=7d]
	$(BENCH_CLI) report $(if $(since),--since $(since))
