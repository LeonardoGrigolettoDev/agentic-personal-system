"""AIOS V1 test battery (docs/CONTRACTS.md §7, docs/ARCHITECTURE.md §24.18 and §14).

Tasks live in bench/tasks/<category>/<key>.yaml. `bench run` submits each one to Hermes (the only agent
runtime), evaluates its check, reads cost/tokens/tier from the Decision Service ledger, reports the outcome
back to the ledger so routing learns from it, and stores a row in `bench_runs`.
"""

CATEGORIES: dict[str, int] = {
    "simple": 10,
    "medium": 10,
    "debugging": 10,
    "refactor": 5,
    "agentic": 5,
    "research": 5,
    "finance": 5,
    "media": 5,
}

TENANTS = ("nitro", "pessoal", "shared")
DOMAINS = ("chief", "engineering", "finance", "projects", "personal", "learning")
