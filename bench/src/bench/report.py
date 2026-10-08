"""Aggregate bench_runs into §14 metrics: success rate and cost per successful task by category x start model.

cost per success = total cost of every run in the group / number of successful runs. The §14 example:
K2.7 at $0.25 per attempt needing 4 attempts costs $1.00 per success; Sonnet at $0.80 with 1 attempt costs
$0.80 per success, so Sonnet is the economically better choice. Rows with an infrastructure `error` are
counted separately and excluded from every rate and cost.
"""

import re
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from bench import CATEGORIES

UNKNOWN = "unknown"


@dataclass
class Stats:
    runs: int = 0
    successes: int = 0
    errors: int = 0
    cost_usd: float = 0.0
    tokens: int = 0
    iterations: int = 0
    escalated: int = 0
    tt_scored: int = 0
    tt_ok: int = 0
    dom_scored: int = 0
    dom_ok: int = 0
    complexities: set[str] = field(default_factory=set)

    def add(self, row: dict) -> None:
        if row.get("error"):
            self.errors += 1
            return
        self.runs += 1
        self.successes += bool(row.get("success"))
        self.cost_usd += float(row.get("cost_usd") or 0)
        self.tokens += int(row.get("input_tokens") or 0) + int(row.get("output_tokens") or 0)
        self.iterations += int(row.get("iterations") or 0)
        self.escalated += int(row.get("escalations") or 0) > 0
        if row.get("router_task_type_ok") is not None:
            self.tt_scored += 1
            self.tt_ok += bool(row["router_task_type_ok"])
        if row.get("router_domain_ok") is not None:
            self.dom_scored += 1
            self.dom_ok += bool(row["router_domain_ok"])
        if row.get("complexity"):
            self.complexities.add(row["complexity"])

    @staticmethod
    def _ratio(num: float, den: int) -> float | None:
        return num / den if den else None

    @property
    def success_rate(self) -> float | None:
        return self._ratio(self.successes, self.runs)

    @property
    def cost_per_success(self) -> float | None:
        return self._ratio(self.cost_usd, self.successes)

    @property
    def avg_tokens(self) -> float | None:
        return self._ratio(self.tokens, self.runs)

    @property
    def avg_iterations(self) -> float | None:
        return self._ratio(self.iterations, self.runs)

    @property
    def escalation_rate(self) -> float | None:
        return self._ratio(self.escalated, self.runs)

    @property
    def router_task_type_accuracy(self) -> float | None:
        return self._ratio(self.tt_ok, self.tt_scored)

    @property
    def router_domain_accuracy(self) -> float | None:
        return self._ratio(self.dom_ok, self.dom_scored)

    def as_dict(self) -> dict:
        return {"runs": self.runs, "successes": self.successes, "errors": self.errors,
                "total_cost_usd": round(self.cost_usd, 6), "success_rate": self.success_rate,
                "cost_per_success_usd": self.cost_per_success, "avg_tokens": self.avg_tokens,
                "avg_iterations": self.avg_iterations, "escalation_rate": self.escalation_rate,
                "router_task_type_accuracy": self.router_task_type_accuracy,
                "router_domain_accuracy": self.router_domain_accuracy}


def model_of(row: dict) -> str:
    return row.get("start_model") or UNKNOWN


def task_type_of(row: dict) -> str:
    return row.get("expected_task_type") or row.get("task_type") or UNKNOWN


def aggregate(rows: Iterable[dict], key: Callable[[dict], tuple]) -> dict[tuple, Stats]:
    groups: dict[tuple, Stats] = defaultdict(Stats)
    for row in rows:
        groups[key(row)].add(row)
    return dict(groups)


def by_category_model(rows: Iterable[dict]) -> dict[tuple, Stats]:
    groups = aggregate(rows, lambda r: (r.get("category") or UNKNOWN, model_of(r)))
    order = {c: i for i, c in enumerate(CATEGORIES)}
    return dict(sorted(groups.items(), key=lambda kv: (order.get(kv[0][0], 99), kv[0][1])))


def by_task_type_model(rows: Iterable[dict]) -> dict[tuple, Stats]:
    return dict(sorted(aggregate(rows, lambda r: (task_type_of(r), model_of(r))).items()))


@dataclass(frozen=True)
class Suggestion:
    task_type: str
    model: str
    success_rate: float
    cost_per_success: float
    runs: int
    complexities: tuple[str, ...]


def suggest(rows: list[dict], target_success: float = 0.8, min_runs: int = 3,
            known_models: set[str] | None = None) -> list[Suggestion]:
    """Per task type: the start model with the lowest cost per success among those with success rate >= target
    and at least `min_runs` verdict runs (ties: higher success rate, then name)."""
    best: dict[str, Suggestion] = {}
    for (task_type, model), st in by_task_type_model(rows).items():
        if task_type == UNKNOWN or model == UNKNOWN or (known_models is not None and model not in known_models):
            continue
        if st.runs < min_runs or st.success_rate is None or st.success_rate < target_success:
            continue
        cand = Suggestion(task_type, model, st.success_rate, st.cost_per_success or 0.0, st.runs,
                          tuple(sorted(st.complexities)))
        cur = best.get(task_type)
        if cur is None or (cand.cost_per_success, -cand.success_rate, cand.model) < \
                (cur.cost_per_success, -cur.success_rate, cur.model):
            best[task_type] = cand
    return [best[k] for k in sorted(best)]


# ---------------------------------------------------------------- rendering

def _pct(v: float | None) -> str:
    return "-" if v is None else f"{v * 100:.1f}%"


def _usd(v: float | None) -> str:
    return "-" if v is None else f"${v:.4f}"


def _num(v: float | None, digits: int = 1) -> str:
    return "-" if v is None else f"{v:,.{digits}f}"


HEADERS = ("runs", "err", "success", "cost/success", "total cost", "avg tokens", "avg iter", "escal.",
           "router type", "router domain")


def table(groups: dict[tuple, Stats], key_names: tuple[str, ...]) -> str:
    lines = [(*key_names, *HEADERS)]
    for key, st in groups.items():
        lines.append((*key, str(st.runs), str(st.errors), _pct(st.success_rate), _usd(st.cost_per_success),
                      _usd(st.cost_usd), _num(st.avg_tokens, 0), _num(st.avg_iterations), _pct(st.escalation_rate),
                      _pct(st.router_task_type_accuracy), _pct(st.router_domain_accuracy)))
    widths = [max(len(row[i]) for row in lines) for i in range(len(lines[0]))]
    nkeys = len(key_names)
    out = []
    for n, row in enumerate(lines):
        cells = [c.ljust(w) if i < nkeys else c.rjust(w) for i, (c, w) in enumerate(zip(row, widths))]
        out.append("  ".join(cells).rstrip())
        if n == 0:
            out.append("  ".join("-" * w for w in widths))
    return "\n".join(out)


_RELATIVE = re.compile(r"^(\d+)([hdw])$")


def parse_since(value: str | None, now: datetime | None = None) -> datetime | None:
    """'7d', '24h', '2w', '2026-10-01' or an ISO timestamp (naive = UTC)."""
    if not value:
        return None
    now = now or datetime.now(UTC)
    if m := _RELATIVE.match(value.strip()):
        n, unit = int(m.group(1)), m.group(2)
        return now - timedelta(hours=n) if unit == "h" else now - timedelta(days=n * (7 if unit == "w" else 1))
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError(f"--since must be like 7d, 24h, 2w, 2026-10-01 or an ISO timestamp: {value!r}") from exc
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
