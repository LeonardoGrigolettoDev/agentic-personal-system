"""Result rows (`bench_runs`, migrations/013_bench.sql) and where they go: Postgres or memory (tests, --no-db)."""

import json
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from typing import Protocol

COLUMNS_SQL = "SELECT to_regclass('public.bench_runs') IS NOT NULL"


class StoreError(Exception):
    pass


@dataclass
class BenchRow:
    run_at: datetime
    batch_id: str
    task_key: str
    category: str
    repeat_index: int
    session_id: str
    tenant: str
    success: bool
    check_type: str
    duration_ms: int
    check_detail: dict = field(default_factory=dict)
    hermes_run_id: str | None = None
    hermes_status: str | None = None
    hermes_profile: str | None = None
    agent: str | None = None
    requested_model: str | None = None
    start_model: str | None = None
    final_model: str | None = None
    tier: int | None = None
    task_type: str | None = None
    expected_task_type: str | None = None
    domain: str | None = None
    expected_domain: str | None = None
    complexity: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0
    iterations: int = 0
    tool_calls: int | None = None
    repairs: int = 0
    escalations: int = 0
    router_task_type_ok: bool | None = None
    router_domain_ok: bool | None = None
    error: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


ROW_FIELDS = [f.name for f in fields(BenchRow)]


class Store(Protocol):
    def insert(self, row: BenchRow) -> None: ...
    def fetch(self, since: datetime | None = None) -> list[dict]: ...


class MemoryStore:
    def __init__(self) -> None:
        self.rows: list[BenchRow] = []

    def insert(self, row: BenchRow) -> None:
        self.rows.append(row)

    def fetch(self, since: datetime | None = None) -> list[dict]:
        return [r.as_dict() for r in self.rows if since is None or r.run_at >= since]


def strip_nul(value):
    """Postgres text and jsonb reject NUL (\\u0000); command output and answers can carry it."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {strip_nul(k): strip_nul(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [strip_nul(v) for v in value]
    return value


class PostgresStore:
    def __init__(self, url: str, connect_timeout: int = 10):
        import psycopg  # imported lazily: validate/report-from-memory never need a driver

        self._psycopg = psycopg
        try:
            self.conn = psycopg.connect(url, autocommit=True, connect_timeout=connect_timeout,
                                        application_name="aios-bench")
        except psycopg.Error as exc:
            raise StoreError(f"cannot connect to Postgres: {exc}") from exc
        if not self.conn.execute(COLUMNS_SQL).fetchone()[0]:
            self.conn.close()
            raise StoreError("table bench_runs is missing: apply migrations/013_bench.sql (make migrate)")

    def insert(self, row: BenchRow) -> None:
        from psycopg.types.json import Jsonb

        values = {k: strip_nul(v) for k, v in row.as_dict().items()}
        values["check_detail"] = Jsonb(values["check_detail"], dumps=lambda v: json.dumps(v, default=str))
        cols = ", ".join(ROW_FIELDS)
        params = ", ".join(f"%({c})s" for c in ROW_FIELDS)
        try:
            self.conn.execute(f"INSERT INTO bench_runs ({cols}) VALUES ({params})", values)
        except self._psycopg.Error as exc:
            raise StoreError(f"insert bench_runs ({row.session_id}): {exc}") from exc

    def fetch(self, since: datetime | None = None) -> list[dict]:
        from psycopg.rows import dict_row

        sql = f"SELECT {', '.join(ROW_FIELDS)} FROM bench_runs"
        args: tuple = ()
        if since is not None:
            sql += " WHERE run_at >= %s"
            args = (since,)
        try:
            with self.conn.cursor(row_factory=dict_row) as cur:
                rows = cur.execute(sql + " ORDER BY run_at, id", args).fetchall()
        except self._psycopg.Error as exc:
            raise StoreError(f"read bench_runs: {exc}") from exc
        for r in rows:
            if r.get("cost_usd") is not None:
                r["cost_usd"] = float(r["cost_usd"])
        return rows

    def close(self) -> None:
        self.conn.close()
