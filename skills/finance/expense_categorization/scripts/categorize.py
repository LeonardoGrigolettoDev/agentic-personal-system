#!/usr/bin/env python3
"""Rule-based expense categorization (keyword -> category) with the leftovers grouped for the LLM.

Input: transactions as CSV (any common delimiter, UTF-8 or Windows-1252) or JSON (array of objects,
or {"transactions": [...]}). Rules: YAML or JSON, format documented in templates/rules.example.yaml:

    categories:
      Alimentação: [ifood, "pão de açúcar", "re:^padaria\\b"]
      Transporte: [uber, posto]

Matching is case- and accent-insensitive on the description field(s). A keyword matches whole words
(``uber`` does not match ``uberlandia``); ``re:`` patterns are regexes over the normalized text. The
longest match wins, ties go to the category listed first. PyYAML is used when installed; otherwise a
built-in parser reads the documented YAML subset.

Amounts: the decimal style (comma or dot) is decided once per amount column by majority vote, with the
same rules as finance/spreadsheet_analysis/scripts/analyze.py, so both scripts read "-1,234" (US
thousands) or "1.234" (BR thousands) the same way; force it with --decimal.

Exit codes: 0 ok, 2 bad input/usage.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import unicodedata
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path

DESC_NAMES = ("descricao", "description", "historico", "memo", "estabelecimento", "merchant", "detalhe",
              "lancamento", "titulo", "payee")
AMOUNT_NAMES = ("valor", "amount", "value", "montante", "total", "quantia", "vlr")


class InputError(Exception):
    pass


# ----------------------------------------------------------------------------- text helpers

def normalize(text: str) -> str:
    """Lowercase, strip accents, collapse whitespace."""
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", plain).strip()


def group_key(text: str) -> str:
    """Cluster recurring merchants: drop digits/punctuation (ids, dates, card suffixes)."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z ]+", " ", normalize(text))).strip() or "(vazio)"


# Number parsing mirrors analyze.py (clean_number / detect_decimal / parse_number); keep them in sync.
CURRENCY = re.compile(r"(R\$|US\$|\$|€|£|BRL|USD|EUR)", re.IGNORECASE)
NUMBER_BODY = re.compile(r"^(\d[\d.,]*|[.,]\d+)$")
COMMA_DECIMAL = re.compile(r"^\d+,\d{1,2}$|^\d{1,3}(\.\d{3})+,\d+$")
DOT_DECIMAL = re.compile(r"^\d+\.\d{1,2}$|^\d{1,3}(,\d{3})+\.\d+$")
DOT_THOUSANDS_ONLY = re.compile(r"^\d{1,3}(\.\d{3})+$")
COMMA_THOUSANDS_ONLY = re.compile(r"^\d{1,3}(,\d{3})+$")


def clean_number(text: str) -> tuple[str, bool] | None:
    """Strip currency/sign decorations -> (unsigned body, negative) or None if not numeric."""
    s = CURRENCY.sub("", (text or "").replace("\u00a0", " ")).strip().replace(" ", "")
    negative = False
    if len(s) > 2 and s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1]
    if len(s) > 1 and s.endswith("-"):
        negative, s = True, s[:-1]
    if len(s) > 1 and s[0] in "+-":
        negative, s = negative or s[0] == "-", s[1:]
    return (s, negative) if NUMBER_BODY.match(s) else None


def detect_decimal(values: list[str]) -> str:
    """'comma' or 'dot' by majority vote over a column; unambiguous shapes count 1, thousands-only 0.5."""
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


def parse_amount(text: str, decimal: str) -> Decimal | None:
    parsed = clean_number(text)
    if not parsed:
        return None
    body, negative = parsed
    normalized = body.replace(".", "").replace(",", ".") if decimal == "comma" else body.replace(",", "")
    try:
        value = Decimal(normalized)
    except InvalidOperation:
        return None
    return -value if negative else value


# ----------------------------------------------------------------------------- rules

def load_rules(path: Path) -> list[tuple[str, str, re.Pattern]]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        data = json.loads(text)
    else:
        try:
            import yaml  # type: ignore[import-untyped]
            data = yaml.safe_load(text)
        except ImportError:
            data = parse_simple_yaml(text)
    if not isinstance(data, dict) or not isinstance(data.get("categories"), dict):
        raise InputError("arquivo de regras precisa de um mapa 'categories: {Categoria: [palavras]}'")
    rules = []
    for category, patterns in data["categories"].items():
        if isinstance(patterns, str):
            patterns = [patterns]
        if not isinstance(patterns, list):
            raise InputError(f"categoria '{category}': esperava uma lista de palavras-chave")
        for raw in patterns:
            raw = str(raw).strip()
            if not raw:
                continue
            if raw.startswith("re:"):
                try:
                    compiled = re.compile(raw[3:])
                except re.error as exc:
                    raise InputError(f"regex inválida em '{category}': {raw} ({exc})") from None
            else:
                compiled = re.compile(r"(?<![a-z0-9])" + re.escape(normalize(raw)) + r"(?![a-z0-9])")
            rules.append((str(category), raw, compiled))
    if not rules:
        raise InputError("nenhuma regra encontrada em 'categories'")
    return rules


def parse_simple_yaml(text: str):
    """YAML subset: nested mappings, block lists, [flow, lists], quoted scalars, # comments."""
    lines = []
    for raw in text.splitlines():
        stripped = _strip_comment(raw).rstrip()
        if stripped.strip():
            lines.append((len(stripped) - len(stripped.lstrip(" ")), stripped.strip()))
    value, pos = _parse_block(lines, 0, lines[0][0] if lines else 0)
    if pos != len(lines):
        raise InputError(f"YAML: indentação inesperada perto de '{lines[pos][1]}'")
    return value


def _strip_comment(line: str) -> str:
    quote = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i]
    return line


def _parse_block(lines, pos, indent):
    if pos >= len(lines):
        return None, pos
    if lines[pos][1].startswith("- ") or lines[pos][1] == "-":
        items = []
        while pos < len(lines) and lines[pos][0] == indent and lines[pos][1].startswith("-"):
            items.append(_scalar(lines[pos][1][1:].strip()))
            pos += 1
        return items, pos
    mapping = {}
    while pos < len(lines) and lines[pos][0] == indent:
        key, rest = _split_key(lines[pos][1])
        pos += 1
        if rest:
            mapping[key] = _scalar(rest)
        elif pos < len(lines) and lines[pos][0] > indent:
            mapping[key], pos = _parse_block(lines, pos, lines[pos][0])
        else:
            mapping[key] = None
    return mapping, pos


def _split_key(content: str) -> tuple[str, str]:
    quote = None
    for i, ch in enumerate(content):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == ":" and (i + 1 == len(content) or content[i + 1] == " "):
            return str(_scalar(content[:i].strip())), content[i + 1:].strip()
    raise InputError(f"YAML: esperava 'chave: valor' em '{content}'")


def _scalar(token: str):
    if token.startswith("[") and token.endswith("]"):
        return [_scalar(part) for part in _split_flow(token[1:-1])]
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "'\"":
        inner = token[1:-1]
        return inner.replace('\\"', '"').replace("\\\\", "\\") if token[0] == '"' else inner.replace("''", "'")
    if token in ("", "~", "null", "Null", "NULL"):
        return None
    if token in ("true", "false"):
        return token == "true"
    if re.fullmatch(r"-?\d+", token):
        return int(token)
    if re.fullmatch(r"-?\d+\.\d+", token):
        return float(token)
    return token


def _split_flow(body: str) -> list[str]:
    parts, current, quote = [], [], None
    for ch in body:
        if quote:
            current.append(ch)
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
            current.append(ch)
        elif ch == ",":
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return [p for p in parts if p]


# ----------------------------------------------------------------------------- transactions

def load_transactions(path: Path, delimiter: str | None) -> tuple[list[str], list[dict]]:
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")
    if path.suffix.lower() in (".json", ".jsonl"):
        if path.suffix.lower() == ".jsonl":
            data = [json.loads(line) for line in text.splitlines() if line.strip()]
        else:
            data = json.loads(text)
        if isinstance(data, dict):
            data = data.get("transactions", data.get("rows"))
        if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
            raise InputError("JSON precisa ser uma lista de objetos (ou {'transactions': [...]})")
        fields = list(dict.fromkeys(k for r in data for k in r))
        return fields, [{k: "" if v is None else str(v) for k, v in r.items()} for r in data]
    if not delimiter:
        try:
            delimiter = csv.Sniffer().sniff(text[:65536], delimiters=",;\t|").delimiter
        except csv.Error:
            first = text.splitlines()[0] if text else ""
            delimiter = max(",;\t|", key=first.count)
    reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=delimiter)
    rows = [r for r in reader if any((v or "").strip() for v in r.values() if isinstance(v, str))]
    return list(reader.fieldnames or []), [{k: (v or "") for k, v in r.items() if k is not None} for r in rows]


def pick_column(fields: list[str], wanted: str | None, candidates: tuple[str, ...], role: str,
                required: bool) -> str | None:
    if wanted:
        if wanted not in fields:
            raise InputError(f"coluna '{wanted}' ({role}) não existe; colunas: {fields}")
        return wanted
    for field in fields:
        norm = re.sub(r"[^a-z0-9]+", "_", normalize(field)).strip("_")
        if any(norm == c or norm.startswith(c + "_") for c in candidates):
            return field
    if required:
        raise InputError(f"não achei a coluna de {role}; use --{role}-col. Colunas: {fields}")
    return None


def categorize(rows: list[dict], fields: list[str], amount_col: str | None, decimal: str,
               rules: list[tuple[str, str, re.Pattern]]) -> list[dict]:
    order = {cat: i for i, cat in enumerate(dict.fromkeys(r[0] for r in rules))}
    results = []
    for n, row in enumerate(rows, start=1):
        text = " | ".join(row.get(f, "") for f in fields if row.get(f))
        norm = normalize(text)
        best = None
        for category, raw, pattern in rules:
            match = pattern.search(norm)
            if match:
                key = (len(match.group(0)), -order[category])
                if best is None or key > best[0]:
                    best = (key, category, raw)
        amount = parse_amount(row.get(amount_col, ""), decimal) if amount_col else None
        results.append({"row": n, "text": text, "amount": amount,
                        "category": best[1] if best else None, "rule": best[2] if best else None})
    return results


def summarize(results: list[dict], top_unknown: int) -> dict:
    def money(value: Decimal) -> float:
        return float(value.quantize(Decimal("0.01")))

    by_cat: dict[str, list] = defaultdict(lambda: [0, Decimal(0)])
    unknown: dict[str, dict] = {}
    for r in results:
        amount = r["amount"] if r["amount"] is not None else Decimal(0)
        if r["category"]:
            by_cat[r["category"]][0] += 1
            by_cat[r["category"]][1] += amount
            continue
        group = unknown.setdefault(group_key(r["text"]), {"count": 0, "total": Decimal(0), "examples": [], "rows": []})
        group["count"] += 1
        group["total"] += amount
        if r["text"] not in group["examples"] and len(group["examples"]) < 3:
            group["examples"].append(r["text"])
        if len(group["rows"]) < 10:
            group["rows"].append(r["row"])
    categorized = sum(v[0] for v in by_cat.values())
    ranked_unknown = sorted(unknown.items(), key=lambda kv: (-kv[1]["count"], -abs(kv[1]["total"]), kv[0]))
    return {
        "total_rows": len(results), "categorized": categorized, "unknown": len(results) - categorized,
        "coverage_pct": round(100 * categorized / len(results), 1) if results else 0.0,
        "by_category": [{"category": k, "count": v[0], "total": money(v[1])}
                        for k, v in sorted(by_cat.items(), key=lambda kv: (-kv[1][0], kv[0]))],
        "unknown_groups": [{"key": k, "count": g["count"], "total": money(g["total"]), "examples": g["examples"],
                            "rows": g["rows"]} for k, g in ranked_unknown[:top_unknown]],
        "unknown_groups_omitted": max(0, len(ranked_unknown) - top_unknown),
    }


def write_csv(path: Path, rows: list[dict], fields: list[str], results: list[dict]) -> None:
    existing = {f.lower() for f in fields}
    cat_col = "categoria" if "categoria" not in existing else "categoria_regra"
    rule_col = "regra" if "regra" not in existing else "regra_aplicada"
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(fields + [cat_col, rule_col])
        for row, res in zip(rows, results, strict=True):
            writer.writerow([row.get(f, "") for f in fields] + [res["category"] or "", res["rule"] or ""])


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Categoriza transações por regras palavra-chave -> categoria.")
    p.add_argument("path", type=Path, help="transações (.csv, .json, .jsonl)")
    p.add_argument("--rules", type=Path, required=True, help="regras (.yaml/.yml/.json)")
    p.add_argument("--desc-col", action="append", dest="desc_cols",
                   help="coluna(s) de texto para casar (repetível; padrão: descrição detectada)")
    p.add_argument("--amount-col", help="coluna de valor (padrão: detectada)")
    p.add_argument("--decimal", choices=("auto", "comma", "dot"), default="auto",
                   help="separador decimal da coluna de valor (padrão: detectado na coluna inteira)")
    p.add_argument("--delimiter", help="separador do CSV (padrão: detectado)")
    p.add_argument("--top-unknown", type=int, default=50, help="grupos de desconhecidos no JSON (padrão 50)")
    p.add_argument("--rows", action="store_true", help="inclui o resultado linha a linha no JSON")
    p.add_argument("--output-csv", type=Path, help="grava as transações com colunas 'categoria' e 'regra'")
    p.add_argument("--pretty", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        for path in (args.path, args.rules):
            if not path.is_file():
                raise InputError(f"arquivo não encontrado: {path}")
        rules = load_rules(args.rules)
        fields, rows = load_transactions(args.path, args.delimiter)
        if args.desc_cols:
            text_fields = [pick_column(fields, c, DESC_NAMES, "desc", True) for c in args.desc_cols]
        else:
            text_fields = [pick_column(fields, None, DESC_NAMES, "desc", True)]
        amount_col = pick_column(fields, args.amount_col, AMOUNT_NAMES, "amount", False)
        decimal = args.decimal
        if decimal == "auto":
            decimal = detect_decimal([row.get(amount_col, "") for row in rows]) if amount_col else "dot"
        results = categorize(rows, text_fields, amount_col, decimal, rules)
        if args.output_csv:
            write_csv(args.output_csv, rows, fields, results)
    except (InputError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    report = {"input": str(args.path), "rules": str(args.rules), "rules_count": len(rules),
              "fields": text_fields, "amount_column": amount_col,
              "amount_decimal": decimal if amount_col else None, **summarize(results, args.top_unknown)}
    if args.output_csv:
        report["output_csv"] = str(args.output_csv)
    if args.rows:
        report["rows"] = [{**r, "amount": float(r["amount"]) if r["amount"] is not None else None} for r in results]
    json.dump(report, sys.stdout, ensure_ascii=False, indent=2 if args.pretty else None)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
