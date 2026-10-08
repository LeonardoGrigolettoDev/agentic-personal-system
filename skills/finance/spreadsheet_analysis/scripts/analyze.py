#!/usr/bin/env python3
"""Profile a CSV/XLSX spreadsheet deterministically and print JSON.

Columns (type, nulls, distinct, min/max/sum), row count, totals, sums by category and by month,
month-over-month deltas, category x month totals and the top-N rows by absolute amount.

Loading: the duckdb CLI (when on PATH) reads CSV/XLSX as text; otherwise the csv module reads CSV
and a stdlib zip/XML reader reads XLSX. Typing, parsing and aggregation always happen here, so both
engines give the same numbers. Bank exports with a preamble ("Extrato...", "Agência...", blank line,
then the header) are handled by both: duckdb's sniffer skips it, the Python reader picks the first
header-like row with the table's usual width (override with --skip-rows N). Brazilian formats are
understood: "1.234,56", "R$ -10,00", "(10,00)", "10,00-", dd/mm/yyyy dates and Excel serial dates
in date-named columns.

Exit codes: 0 ok, 2 bad input/usage.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import shutil
import subprocess
import sys
import unicodedata
import zipfile
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from itertools import islice
from pathlib import Path
from xml.etree import ElementTree

# Matched against normalized column names (accents stripped, snake_case). Each role lists patterns in
# priority order: a column matching an earlier pattern wins over any column matching a later one. A bare
# "Tipo" column is not a category: in bank statements it is the payment type (PIX, TED, Débito).
ROLE_PATTERNS = {
    "date": (r"^(data|date|dt|dia|vencimento|competencia|posted|transaction_date)(_|$)",),
    "amount": (r"^(valor|amount|value|montante|total|quantia|preco|price|vlr)(_|$)",),
    "debit": (r"^(debito|debit|saida|saidas|despesa|despesas)(_|$)",),
    "credit": (r"^(credito|credit|entrada|entradas|receita|receitas)(_|$)",),
    "category": (r"^(categoria|category)(_|$)",
                 r"^(subcategoria|subcategory|classe|grupo|centro_de_custo)(_|$)",
                 r"^tipo_(de_)?(despesa|gasto|receita|lancamento)(_|$)"),
    "description": (r"^(descricao|description|historico|memo|estabelecimento|merchant|detalhe|lancamento|titulo"
                    r"|payee)(_|$)",),
}
DELIMITERS = (",", ";", "\t", "|")
HEADER_SCAN_ROWS = 50  # a preamble longer than this is not detected (use --skip-rows)
CURRENCY = re.compile(r"(R\$|US\$|\$|€|£|BRL|USD|EUR)", re.IGNORECASE)
NUMBER_BODY = re.compile(r"^(\d[\d.,]*|[.,]\d+)$")
SCIENTIFIC = re.compile(r"^[+-]?\d+(\.\d+)?[eE][+-]?\d+$")
COMMA_DECIMAL = re.compile(r"^\d+,\d{1,2}$|^\d{1,3}(\.\d{3})+,\d+$")
DOT_DECIMAL = re.compile(r"^\d+\.\d{1,2}$|^\d{1,3}(,\d{3})+\.\d+$")
DOT_THOUSANDS_ONLY = re.compile(r"^\d{1,3}(\.\d{3})+$")
COMMA_THOUSANDS_ONLY = re.compile(r"^\d{1,3}(,\d{3})+$")
DATE_FORMATS = {
    "dmy": ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y/%m/%d", "%d/%m/%y"),
    "mdy": ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%Y/%m/%d", "%m/%d/%y"),
}
EXCEL_EPOCH = date(1899, 12, 30)
TYPE_THRESHOLD = 0.9
NS_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


class InputError(Exception):
    pass


# ----------------------------------------------------------------------------- loading

def load_table(path: Path, engine: str, sheet: str | None, delimiter: str | None, skip_rows: int | None,
               max_rows: int, warnings: list[str]) -> tuple[list[str], list[list[str]], str]:
    is_xlsx = path.suffix.lower() in (".xlsx", ".xlsm")
    # read_xlsx has no "skip lines" option: an explicit --skip-rows on XLSX uses the Python reader
    duckdb_ok = not (is_xlsx and skip_rows is not None)
    if engine in ("auto", "duckdb") and shutil.which("duckdb") and duckdb_ok:
        try:
            header, rows = load_with_duckdb(path, is_xlsx, sheet, delimiter, skip_rows, max_rows)
            return header, rows, "duckdb"
        except InputError as exc:
            if engine == "duckdb":
                raise
            warnings.append(f"duckdb falhou ({exc}); usando o leitor Python")
    elif engine == "duckdb":
        raise InputError("--engine duckdb pedido, mas o CLI duckdb não está no PATH" if duckdb_ok
                         else "--skip-rows em XLSX só funciona com --engine python")
    if is_xlsx:
        header, rows = read_xlsx(path, sheet, skip_rows, max_rows, warnings)
    else:
        header, rows = read_csv(path, delimiter, skip_rows, max_rows, warnings)
    return header, rows, "python"


def load_with_duckdb(path: Path, is_xlsx: bool, sheet: str | None, delimiter: str | None, skip_rows: int | None,
                     max_rows: int) -> tuple[list[str], list[list[str]]]:
    def lit(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    if is_xlsx:
        opts = ["all_varchar=true", "header=true"] + ([f"sheet={lit(sheet)}"] if sheet else [])
        source = f"read_xlsx({lit(str(path))}, {', '.join(opts)})"
    else:
        opts = ["all_varchar=true", "header=true"] + ([f"delim={lit(delimiter)}"] if delimiter else [])
        opts += [f"skip={skip_rows}"] if skip_rows is not None else []
        source = f"read_csv({lit(str(path))}, {', '.join(opts)})"
    columns = [c["column_name"] for c in run_duckdb(f"DESCRIBE SELECT * FROM {source}")]
    data = run_duckdb(f"SELECT * FROM {source} LIMIT {max_rows + 1}")
    rows = [["" if r.get(c) is None else str(r.get(c)) for c in columns] for r in data]
    return columns, rows


def run_duckdb(sql: str) -> list[dict]:
    try:
        proc = subprocess.run(["duckdb", "-json", "-c", sql], capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InputError(str(exc)) from None
    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout or "").strip()
        raise InputError(message.splitlines()[0] if message else f"duckdb exit {proc.returncode}")
    out = proc.stdout.strip()
    return json.loads(out) if out else []


def read_csv(path: Path, delimiter: str | None, skip_rows: int | None, max_rows: int,
             warnings: list[str]) -> tuple[list[str], list[list[str]]]:
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")
        warnings.append("arquivo não é UTF-8; lido como cp1252 (Windows-1252)")
    delimiter = delimiter or sniff_delimiter(text[:65536])
    lines = list(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter))  # [] for blank lines
    return split_header(lines, skip_rows, max_rows, warnings)


def sniff_delimiter(sample: str) -> str:
    """The delimiter whose parsed rows most often share one width (>= 2 columns); ties -> wider rows.
    Counting parsed widths, not raw characters, keeps quoted "1,234.50" and BR decimal commas from
    outvoting ';' — the header row has no decimal commas, so ';' always wins on a BR export."""
    best, best_score = ",", (0, 0)
    for delim in DELIMITERS:
        try:
            widths = Counter(len(row) for row in csv.reader(io.StringIO(sample, newline=""), delimiter=delim)
                             if len(row) >= 2 and any(cell.strip() for cell in row))
        except csv.Error:
            continue
        if widths:
            width, freq = widths.most_common(1)[0]
            if (freq, width) > best_score:
                best, best_score = delim, (freq, width)
    return best


def split_header(lines: list[list[str]], skip_rows: int | None, max_rows: int,
                 warnings: list[str]) -> tuple[list[str], list[list[str]]]:
    """Header + data rows. skip_rows counts raw lines (blank ones too), like duckdb's ``skip``."""
    if skip_rows is not None:
        start = skip_rows
    else:
        start = find_header(lines)
        skipped = sum(1 for row in lines[:start] if any(cell.strip() for cell in row))
        if skipped:
            warnings.append(f"{skipped} linha(s) de preâmbulo antes do cabeçalho ignorada(s) "
                            f"(cabeçalho na linha {start + 1}); ajuste com --skip-rows")
    rows = [row for row in lines[start:] if any(cell.strip() for cell in row)]
    if not rows:
        raise InputError("arquivo vazio" if skip_rows is None else f"nada depois de --skip-rows {skip_rows}")
    return rows[0], rows[1:max_rows + 2]


def find_header(lines: list[list[str]]) -> int:
    """Index of the header line: the first non-blank line, unless it is narrower than the table's usual
    width — then the first header-like line (no numbers, no dates) that has that width."""
    candidates = list(islice(((i, row) for i, row in enumerate(lines) if any(cell.strip() for cell in row)),
                             HEADER_SCAN_ROWS * 4))
    if not candidates:
        return 0
    widths = Counter(_width(row) for _, row in candidates if _width(row) >= 2)
    first_index, first = candidates[0]
    if not widths:
        return first_index
    usual = widths.most_common(1)[0][0]
    if _width(first) >= usual:
        return first_index
    for index, row in candidates[:HEADER_SCAN_ROWS]:
        if _width(row) == usual and _looks_like_header(row):
            return index
    return first_index


def _width(row: list[str]) -> int:
    """Cells up to the last non-empty one (trailing empty cells from ';;' or XLSX padding don't count)."""
    width = len(row)
    while width and not row[width - 1].strip():
        width -= 1
    return width


def _looks_like_header(row: list[str]) -> bool:
    cells = [cell.strip() for cell in row if cell.strip()]
    return len(cells) >= 2 and not any(clean_number(c) or parse_date(c, "dmy", False) for c in cells)


def read_xlsx(path: Path, sheet: str | None, skip_rows: int | None, max_rows: int,
              warnings: list[str]) -> tuple[list[str], list[list[str]]]:
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise InputError(f"XLSX inválido: {exc}") from None
    with zf:
        target = _xlsx_sheet_path(zf, sheet)
        shared = _xlsx_shared_strings(zf)
        grid: list[list[str]] = []
        with zf.open(target) as fh:
            for _, elem in ElementTree.iterparse(fh):
                if _local(elem.tag) != "row":
                    continue
                cells: dict[int, str] = {}
                for c in elem:
                    if _local(c.tag) == "c":
                        cells[_column_index(c.get("r", ""), len(cells))] = _xlsx_cell(c, shared)
                number = elem.get("r", "")
                elem.clear()
                if number.isdigit():  # rows absent from the XML are blank: keep them so --skip-rows counts them
                    grid.extend([] for _ in range(int(number) - 1 - len(grid)))
                width = max(cells) + 1 if cells else 0
                grid.append([cells.get(i, "") for i in range(width)])
                if len(grid) > max_rows + HEADER_SCAN_ROWS + (skip_rows or 0) + 1:
                    break
    if not any(any(v.strip() for v in row) for row in grid):
        raise InputError("planilha vazia")
    return split_header(grid, skip_rows, max_rows, warnings)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _column_index(ref: str, fallback: int) -> int:
    letters = "".join(ch for ch in ref if ch.isalpha()).upper()
    if not letters:
        return fallback
    index = 0
    for ch in letters:
        index = index * 26 + (ord(ch) - 64)
    return index - 1


def _xlsx_sheet_path(zf: zipfile.ZipFile, sheet: str | None) -> str:
    workbook = ElementTree.fromstring(zf.read("xl/workbook.xml"))
    sheets = [s for s in workbook.iter() if _local(s.tag) == "sheet"]
    if not sheets:
        raise InputError("XLSX sem planilhas")
    chosen = sheets[0]
    if sheet:
        matches = [s for s in sheets if s.get("name") == sheet]
        if not matches:
            names = ", ".join(s.get("name", "?") for s in sheets)
            raise InputError(f"aba '{sheet}' não encontrada (abas: {names})")
        chosen = matches[0]
    rid = chosen.get(NS_REL)
    rels = ElementTree.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    for rel in rels:
        if rel.get("Id") == rid:
            target = rel.get("Target", "")
            return target.lstrip("/") if target.startswith("/") else "xl/" + target
    raise InputError("relacionamento da aba não encontrado no XLSX")


def _xlsx_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    root = ElementTree.fromstring(zf.read("xl/sharedStrings.xml"))
    return ["".join(t.text or "" for t in si.iter() if _local(t.tag) == "t")
            for si in root if _local(si.tag) == "si"]


def _xlsx_cell(c: ElementTree.Element, shared: list[str]) -> str:
    kind = c.get("t", "n")
    if kind == "inlineStr":
        return "".join(t.text or "" for t in c.iter() if _local(t.tag) == "t")
    value = next((v.text or "" for v in c if _local(v.tag) == "v"), "")
    if kind == "s":
        try:
            return shared[int(value)]
        except (ValueError, IndexError):
            return ""
    if kind == "b":
        return "TRUE" if value == "1" else "FALSE"
    return value


# ----------------------------------------------------------------------------- parsing

def normalize_name(name: str) -> str:
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower().strip()
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


def clean_number(text: str) -> tuple[str, bool] | None:
    """Strip currency/sign decorations -> (unsigned body, negative) or None if not numeric."""
    s = CURRENCY.sub("", text.replace("\u00a0", " ")).strip().replace(" ", "")
    negative = False
    if len(s) > 2 and s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1]
    if len(s) > 1 and s.endswith("-"):
        negative, s = True, s[:-1]
    if len(s) > 1 and s[0] in "+-":
        negative, s = negative or s[0] == "-", s[1:]
    if SCIENTIFIC.match(s):
        return s, negative
    return (s, negative) if NUMBER_BODY.match(s) else None


def detect_decimal(values: list[str]) -> str:
    comma = dot = 0.0
    for v in values:
        parsed = clean_number(v)
        if not parsed:
            continue
        body = parsed[0]
        if "," in body and "." in body:
            comma, dot = (comma + 1, dot) if body.rfind(",") > body.rfind(".") else (comma, dot + 1)
        elif COMMA_DECIMAL.match(body):
            comma += 1
        elif DOT_DECIMAL.match(body):
            dot += 1
        elif DOT_THOUSANDS_ONLY.match(body):
            comma += 0.5
        elif COMMA_THOUSANDS_ONLY.match(body):
            dot += 0.5
    return "comma" if comma > dot else "dot"


def parse_number(text: str, decimal: str) -> Decimal | None:
    parsed = clean_number(text)
    if not parsed:
        return None
    body, negative = parsed
    if SCIENTIFIC.match(body):
        normalized = body
    elif decimal == "comma":
        normalized = body.replace(".", "").replace(",", ".")
    else:
        normalized = body.replace(",", "")
    try:
        value = Decimal(normalized)
    except InvalidOperation:
        return None
    return -value if negative else value


def parse_date(text: str, order: str, allow_serial: bool) -> date | None:
    s = text.strip()
    if not s:
        return None
    if allow_serial and re.fullmatch(r"\d{5}(\.\d+)?", s):
        serial = float(s)
        if 20000 <= serial <= 80000:
            return EXCEL_EPOCH + timedelta(days=int(serial))
    candidate = re.split(r"[T ]", s, maxsplit=1)[0]
    for fmt in DATE_FORMATS[order]:
        try:
            return datetime.strptime(candidate, fmt).date()
        except ValueError:
            continue
    return None


# ----------------------------------------------------------------------------- profiling

def money(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01")))


def profile_columns(header: list[str], rows: list[list[str]], decimal_opt: str, date_order: str) -> list[dict]:
    columns = []
    for idx, name in enumerate(header):
        values = [row[idx].strip() if idx < len(row) else "" for row in rows]
        present = [v for v in values if v]
        decimal = decimal_opt if decimal_opt != "auto" else detect_decimal(present)
        date_hint = role_rank("date", name) is not None
        col = {"name": name, "index": idx, "non_null": len(present), "nulls": len(values) - len(present),
               "distinct": len(set(present)), "decimal": decimal}
        dates = [parse_date(v, date_order, date_hint) for v in present]
        numbers = [parse_number(v, decimal) for v in present]
        n_dates = sum(d is not None for d in dates)
        n_numbers = sum(n is not None for n in numbers)
        if not present:
            col["type"] = "empty"
        elif n_dates >= TYPE_THRESHOLD * len(present) and (date_hint or n_dates > n_numbers):
            ok = [d for d in dates if d]
            col.update(type="date", min=min(ok).isoformat(), max=max(ok).isoformat(), invalid=len(present) - n_dates)
        elif n_numbers >= TYPE_THRESHOLD * len(present):
            ok = [n for n in numbers if n is not None]
            col.update(type="number", min=money(min(ok)), max=money(max(ok)), sum=money(sum(ok, Decimal(0))),
                       mean=money(sum(ok, Decimal(0)) / len(ok)), invalid=len(present) - n_numbers)
        else:
            col.update(type="text", top_values=[{"value": v, "count": c} for v, c in Counter(present).most_common(5)])
        columns.append(col)
    return columns


def role_rank(role: str, column_name: str) -> int | None:
    """Priority of the first ROLE_PATTERNS[role] entry the column name matches (lower wins), or None."""
    name = normalize_name(column_name)
    return next((rank for rank, pattern in enumerate(ROLE_PATTERNS[role]) if re.match(pattern, name)), None)


def detect_roles(columns: list[dict], overrides: dict[str, str | None]) -> dict[str, dict | None]:
    by_name = {c["name"]: c for c in columns}
    roles: dict[str, dict | None] = {}
    for role in ROLE_PATTERNS:
        wanted = overrides.get(role)
        if wanted:
            if wanted not in by_name:
                raise InputError(f"coluna '{wanted}' (--{role}-col) não existe; colunas: {list(by_name)}")
            roles[role] = by_name[wanted]
            continue
        expected = {"date": "date", "amount": "number", "debit": "number", "credit": "number"}.get(role)
        ranked = [(rank, c["index"], c) for c in columns
                  if (rank := role_rank(role, c["name"])) is not None and (expected is None or c["type"] == expected)]
        roles[role] = min(ranked, key=lambda t: t[:2])[2] if ranked else None
    if roles["date"] is None:
        roles["date"] = next((c for c in columns if c["type"] == "date"), None)
    if roles["amount"] is None and not (roles["debit"] and roles["credit"]):
        taken = {id(c) for c in roles.values() if c}
        numeric = [c for c in columns if c["type"] == "number" and id(c) not in taken]
        roles["amount"] = max(numeric, key=lambda c: c["non_null"], default=None)
    return roles


def row_amount(row: list[str], roles: dict) -> Decimal | None:
    def cell(col: dict | None) -> str:
        return row[col["index"]].strip() if col and col["index"] < len(row) else ""

    if roles["amount"]:
        return parse_number(cell(roles["amount"]), roles["amount"]["decimal"])
    credit = parse_number(cell(roles["credit"]), roles["credit"]["decimal"]) or Decimal(0)
    debit = parse_number(cell(roles["debit"]), roles["debit"]["decimal"]) or Decimal(0)
    if not cell(roles["credit"]) and not cell(roles["debit"]):
        return None
    return credit - abs(debit)


def aggregate(rows: list[list[str]], roles: dict, date_order: str, top_n: int, max_categories: int,
              warnings: list[str]) -> dict:
    def cell(row: list[str], col: dict | None) -> str:
        return row[col["index"]].strip() if col and col["index"] < len(row) else ""

    date_col, cat_col, desc_col = roles["date"], roles["category"], roles["description"]
    totals = {"count": 0, "sum": Decimal(0), "inflow": Decimal(0), "outflow": Decimal(0)}
    by_cat: dict[str, list] = defaultdict(lambda: [Decimal(0), 0])
    by_month: dict[str, dict] = defaultdict(lambda: {"total": Decimal(0), "inflow": Decimal(0),
                                                     "outflow": Decimal(0), "count": 0})
    cat_month: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    bad_amount: list[int] = []
    bad_date: list[int] = []
    scored: list[tuple[Decimal, int, dict]] = []
    for i, row in enumerate(rows, start=1):
        amount = row_amount(row, roles)
        if amount is None:
            if any(c.strip() for c in row):
                bad_amount.append(i)
            continue
        totals["count"] += 1
        totals["sum"] += amount
        totals["inflow" if amount > 0 else "outflow"] += amount
        category = (cell(row, cat_col) or "(sem categoria)") if cat_col else None
        if category is not None:
            by_cat[category][0] += amount
            by_cat[category][1] += 1
        month = None
        if date_col:
            parsed = parse_date(cell(row, date_col), date_order, True)
            if parsed is None:
                bad_date.append(i)
            else:
                month = parsed.strftime("%Y-%m")
                bucket = by_month[month]
                bucket["total"] += amount
                bucket["inflow" if amount > 0 else "outflow"] += amount
                bucket["count"] += 1
                if category is not None:
                    cat_month[category][month] += amount
        scored.append((abs(amount), i, {"row": i, "date": cell(row, date_col) or None,
                                        "description": cell(row, desc_col) or None,
                                        "category": category, "amount": money(amount)}))
    if bad_amount:
        warnings.append(f"{len(bad_amount)} linha(s) sem valor numérico (ex.: linhas {bad_amount[:5]})")
    if bad_date:
        warnings.append(f"{len(bad_date)} linha(s) com data inválida (ex.: linhas {bad_date[:5]})")

    result: dict = {"totals": {"count": totals["count"], "sum": money(totals["sum"]),
                               "inflow": money(totals["inflow"]), "outflow": money(totals["outflow"])}}
    if cat_col:
        ranked = sorted(by_cat.items(), key=lambda kv: (-abs(kv[1][0]), kv[0]))
        result["by_category"] = [{"category": k, "total": money(v[0]), "count": v[1]} for k, v in ranked]
    if date_col:
        months = sorted(by_month)
        result["by_month"] = [{"month": m, "total": money(by_month[m]["total"]), "inflow": money(by_month[m]["inflow"]),
                               "outflow": money(by_month[m]["outflow"]), "count": by_month[m]["count"]} for m in months]
        result["mom"] = month_over_month(months, by_month)
        if cat_col:
            keep = [k for k, _ in sorted(by_cat.items(), key=lambda kv: (-abs(kv[1][0]), kv[0]))][:max_categories]
            if len(by_cat) > max_categories:
                warnings.append(f"by_category_month limitado às {max_categories} maiores categorias")
            result["by_category_month"] = {"months": months, "categories": [
                {"category": k, "totals": {m: money(cat_month[k][m]) for m in months if m in cat_month[k]}}
                for k in keep]}
    result["top"] = [item for _, _, item in sorted(scored, key=lambda t: (-t[0], t[1]))[:top_n]]
    return result


def month_over_month(months: list[str], by_month: dict) -> list[dict]:
    out = []
    for prev, cur in zip(months, months[1:], strict=False):
        p, c = by_month[prev]["total"], by_month[cur]["total"]
        out.append({"month": cur, "previous": prev, "total": money(c), "delta": money(c - p),
                    "delta_pct": round(float((c - p) / abs(p) * 100), 1) if p else None,
                    "gap": _months_between(prev, cur) > 1})
    return out


def _months_between(a: str, b: str) -> int:
    ya, ma = map(int, a.split("-"))
    yb, mb = map(int, b.split("-"))
    return (yb - ya) * 12 + (mb - ma)


def dedupe_header(header: list[str]) -> list[str]:
    seen: Counter = Counter()
    out = []
    for i, raw in enumerate(header):
        name = raw.strip() or f"col_{i + 1}"
        seen[name] += 1
        out.append(name if seen[name] == 1 else f"{name}_{seen[name]}")
    return out


def export_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)


# ----------------------------------------------------------------------------- CLI

def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Perfil determinístico de planilha CSV/XLSX (saída JSON).")
    p.add_argument("path", type=Path)
    p.add_argument("--sheet", help="aba do XLSX (padrão: a primeira)")
    p.add_argument("--engine", choices=("auto", "duckdb", "python"), default="auto")
    p.add_argument("--delimiter", help="separador do CSV (padrão: detectado)")
    p.add_argument("--skip-rows", type=int, metavar="N",
                   help="linhas antes do cabeçalho a pular (padrão: preâmbulo detectado)")
    p.add_argument("--decimal", choices=("auto", "comma", "dot"), default="auto")
    p.add_argument("--date-order", choices=("dmy", "mdy"), default="dmy")
    for role in ("date", "amount", "category", "description", "debit", "credit"):
        p.add_argument(f"--{role}-col", dest=f"{role}_col", help=f"nome exato da coluna de {role}")
    p.add_argument("--top", type=int, default=10, help="linhas de maior valor absoluto (padrão 10)")
    p.add_argument("--max-categories", type=int, default=200)
    p.add_argument("--max-rows", type=int, default=1_000_000)
    p.add_argument("--export-csv", type=Path, help="grava a tabela normalizada (UTF-8, vírgula) neste caminho")
    p.add_argument("--pretty", action="store_true")
    return p.parse_args(argv)


def run(args: argparse.Namespace) -> dict:
    if not args.path.is_file():
        raise InputError(f"arquivo não encontrado: {args.path}")
    if args.skip_rows is not None and args.skip_rows < 0:
        raise InputError("--skip-rows deve ser >= 0")
    warnings: list[str] = []
    header, rows, engine = load_table(args.path, args.engine, args.sheet, args.delimiter, args.skip_rows,
                                      args.max_rows, warnings)
    header = dedupe_header(header)
    if len(rows) > args.max_rows:
        rows = rows[:args.max_rows]
        warnings.append(f"truncado em {args.max_rows} linhas (--max-rows)")
    if args.export_csv:
        export_csv(args.export_csv, header, rows)
    columns = profile_columns(header, rows, args.decimal, args.date_order)
    overrides = {r: getattr(args, f"{r}_col") for r in ROLE_PATTERNS}
    roles = detect_roles(columns, overrides)
    report: dict = {
        "file": str(args.path), "engine": engine, "sheet": args.sheet, "row_count": len(rows),
        "columns": [{k: v for k, v in c.items() if k != "index" and (k != "decimal" or c["type"] == "number")}
                    for c in columns],
        "detected": {role: (c["name"] if c else None) for role, c in roles.items()},
    }
    if roles["amount"] or (roles["debit"] and roles["credit"]):
        report.update(aggregate(rows, roles, args.date_order, args.top, args.max_categories, warnings))
    else:
        warnings.append("nenhuma coluna de valor detectada; use --amount-col (ou --debit-col/--credit-col)")
    if args.export_csv:
        report["exported_csv"] = str(args.export_csv)
    report["warnings"] = warnings
    return report


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        report = run(args)
    except InputError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    json.dump(report, sys.stdout, ensure_ascii=False, indent=2 if args.pretty else None)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
