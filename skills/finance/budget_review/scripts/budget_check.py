#!/usr/bin/env python3
"""Compare actual spending per category (from analyze.py JSON) against a monthly budget.

Budget file: JSON object or flat YAML mapping ``Categoria: limite_mensal`` (positive numbers), e.g.

    Alimentação: 1500
    Moradia: 2.000,00        # Brazilian style: "." thousands, "," decimals (R$ allowed)
    "Lazer & Cultura": 400

Limits written as text follow Brazilian conventions: ``1.500`` is one thousand five hundred, ``1500,50``
and ``1.500,50`` have centavos, ``1500.50`` (dot + 1-2 digits) is also read as centavos. ``1,500`` is
ambiguous and rejected. JSON numbers are taken as they are.

Input: the JSON printed by finance/spreadsheet_analysis/scripts/analyze.py (needs ``by_category_month``,
so the spreadsheet must have date and category columns), as a file path or ``-`` for stdin.

Spending sign: ``auto`` treats negative totals as expenses when most category totals are negative
(bank-statement style), otherwise positive values are expenses (expense-list style).

Exit codes: 0 ok, 2 bad input/usage.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path


class InputError(Exception):
    pass


def load_budget(path: Path) -> dict[str, Decimal]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        data = json.loads(text)
    else:
        data = parse_flat_yaml(text)
    if isinstance(data, dict) and isinstance(data.get("budget"), dict):
        data = data["budget"]
    if not isinstance(data, dict) or not data:
        raise InputError("orçamento precisa ser um mapa não vazio 'Categoria: limite'")
    budget = {}
    for category, limit in data.items():
        value = parse_limit(str(category), limit)
        if value < 0:
            raise InputError(f"limite negativo para '{category}'")
        budget[str(category)] = value
    return budget


BR_THOUSANDS = re.compile(r"\d{1,3}(\.\d{3})+(,\d{1,2})?")   # 1.500 · 1.500,00 · 12.345.678,9
BR_DECIMAL = re.compile(r"\d+,\d{1,2}")                     # 1500,00
PLAIN = re.compile(r"\d+(\.\d{1,2})?")                     # 1500 · 1500.5 · 1500.50
US_THOUSANDS = re.compile(r"\d{1,3}(,\d{3})+\.\d{1,2}")      # 1,500.00 (unambiguous: both separators)


def parse_limit(category: str, raw: object) -> Decimal:
    if isinstance(raw, bool) or raw is None:
        raise InputError(f"limite inválido para '{category}': {raw!r}")
    if isinstance(raw, (int, float)):
        return Decimal(str(raw))
    text = re.sub(r"(R\$|\s)", "", str(raw).replace("\u00a0", ""))
    negative = text.startswith("-")
    body = text.lstrip("+-")
    if BR_THOUSANDS.fullmatch(body) or BR_DECIMAL.fullmatch(body):
        body = body.replace(".", "").replace(",", ".")
    elif US_THOUSANDS.fullmatch(body):
        body = body.replace(",", "")
    elif not PLAIN.fullmatch(body):
        hint = " (ambíguo: escreva 1500 ou 1.500)" if re.fullmatch(r"\d{1,3}(,\d{3})+", body) else ""
        raise InputError(f"limite inválido para '{category}': {raw!r}{hint}")
    try:
        value = Decimal(body)
    except InvalidOperation:
        raise InputError(f"limite inválido para '{category}': {raw!r}") from None
    return -value if negative else value


def parse_flat_yaml(text: str) -> dict[str, str]:
    """One level ``key: number`` (optionally under a ``budget:`` header), quotes and # comments."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = re.sub(r"(^|\s)#.*$", "", raw).strip()
        if not line or line in ("budget:", "---"):
            continue
        m = re.fullmatch(r"""(?:"([^"]+)"|'([^']+)'|([^:]+?))\s*:\s*(\S.*)""", line)
        if not m:
            raise InputError(f"linha de orçamento inválida: '{raw.strip()}'")
        out[m.group(1) or m.group(2) or m.group(3).strip()] = m.group(4).strip().strip("'\"")
    return out


def load_analysis(source: str) -> dict:
    text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    data = json.loads(text)
    bcm = data.get("by_category_month") if isinstance(data, dict) else None
    if not isinstance(bcm, dict) or not bcm.get("months"):
        raise InputError("análise sem 'by_category_month': rode analyze.py numa planilha com colunas "
                         "de data e categoria")
    return data


def pick_sign(categories: list[dict], month: str, mode: str) -> int:
    """-1 when expenses are negative numbers, +1 when they are positive."""
    if mode != "auto":
        return -1 if mode == "negative" else 1
    totals = [Decimal(str(c["totals"][month])) for c in categories if month in c["totals"]]
    negatives = sum(1 for t in totals if t < 0)
    return -1 if negatives >= len(totals) / 2 else 1


def check(analysis: dict, budget: dict[str, Decimal], month: str | None, warn_ratio: Decimal,
          sign_mode: str) -> dict:
    bcm = analysis["by_category_month"]
    month = month or bcm["months"][-1]
    if month not in bcm["months"]:
        raise InputError(f"mês {month} ausente na análise; meses: {bcm['months']}")
    sign = pick_sign(bcm["categories"], month, sign_mode)
    spent: dict[str, Decimal] = {}
    for cat in bcm["categories"]:
        if month in cat["totals"]:
            spent[cat["category"]] = max(Decimal(0), Decimal(str(cat["totals"][month])) * sign)

    rows = []
    for category, limit in budget.items():
        used = spent.get(category, Decimal(0))
        ratio = (used / limit) if limit else (Decimal(1) if used == 0 else Decimal("Infinity"))
        status = "over" if ratio > 1 else "warn" if ratio >= warn_ratio else "ok"
        rows.append({"category": category, "budget": _money(limit), "spent": _money(used),
                     "remaining": _money(limit - used),
                     "used_pct": None if ratio == Decimal("Infinity") else round(float(ratio * 100), 1),
                     "status": status})
    rows.sort(key=lambda r: ({"over": 0, "warn": 1, "ok": 2}[r["status"]],
                            -(r["used_pct"] if r["used_pct"] is not None else 1e9), r["category"]))
    unbudgeted = sorted(({"category": c, "spent": _money(v)} for c, v in spent.items() if c not in budget and v > 0),
                        key=lambda r: -r["spent"])
    total_budget = sum(budget.values(), Decimal(0))
    total_spent = sum((spent.get(c, Decimal(0)) for c in budget), Decimal(0))
    return {
        "month": month, "expense_sign": "negative" if sign < 0 else "positive", "warn_ratio": float(warn_ratio),
        "categories": rows, "unbudgeted": unbudgeted,
        "unused_budget": [r["category"] for r in rows if r["spent"] == 0],
        "totals": {"budget": _money(total_budget), "spent_budgeted": _money(total_spent),
                   "spent_unbudgeted": _money(sum((Decimal(str(u["spent"])) for u in unbudgeted), Decimal(0))),
                   "remaining": _money(total_budget - total_spent)},
        "counts": {s: sum(1 for r in rows if r["status"] == s) for s in ("over", "warn", "ok")},
    }


def _money(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01")))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Orçado x realizado por categoria a partir do JSON do analyze.py.")
    p.add_argument("analysis", help="JSON do analyze.py (ou '-' para stdin)")
    p.add_argument("--budget", type=Path, required=True, help="orçamento mensal (.yaml/.yml/.json)")
    p.add_argument("--month", help="YYYY-MM (padrão: último mês da análise)")
    p.add_argument("--warn-ratio", type=Decimal, default=Decimal("0.8"), help="alerta a partir de (padrão 0.8)")
    p.add_argument("--sign", choices=("auto", "negative", "positive"), default="auto",
                   help="sinal das despesas na planilha (padrão: auto)")
    p.add_argument("--pretty", action="store_true")
    args = p.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if not args.budget.is_file():
            raise InputError(f"arquivo não encontrado: {args.budget}")
        if args.month and not re.fullmatch(r"\d{4}-\d{2}", args.month):
            raise InputError("--month deve ser YYYY-MM")
        result = check(load_analysis(args.analysis), load_budget(args.budget), args.month, args.warn_ratio, args.sign)
    except (InputError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2 if args.pretty else None)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
