"""finance/* scripts on fixtures with known totals (stdlib engine; duckdb parity when the CLI exists)."""

from __future__ import annotations

import csv
import importlib.util
import json
import os
import shutil
import sys
import zipfile
from pathlib import Path

import pytest
import yaml
from skilltest import FIXTURES, run, run_json, script

ANALYZE = script("finance/spreadsheet_analysis/scripts/analyze.py")
BUDGET = script("finance/budget_review/scripts/budget_check.py")
CATEGORIZE = script("finance/expense_categorization/scripts/categorize.py")
RULES_TEMPLATE = script("finance/expense_categorization/templates/rules.example.yaml")
PY = sys.executable


def analyze(*args: str) -> dict:
    return run_json([PY, str(ANALYZE), *args])


# ----------------------------------------------------------------------------- analyze.py

def test_analyze_brazilian_csv_known_totals():
    out = analyze(str(FIXTURES / "transactions_br.csv"), "--engine", "python", "--top", "3")
    assert out["engine"] == "python" and out["row_count"] == 9 and out["warnings"] == []
    assert out["detected"] == {"date": "Data", "amount": "Valor", "debit": None, "credit": None,
                               "category": "Categoria", "description": "Descrição"}
    cols = {c["name"]: c for c in out["columns"]}
    assert (cols["Data"]["type"], cols["Data"]["min"], cols["Data"]["max"]) == ("date", "2026-08-01", "2026-09-21")
    assert cols["Valor"]["type"] == "number" and cols["Valor"]["decimal"] == "comma"
    assert cols["Categoria"]["nulls"] == 1 and cols["Descrição"]["type"] == "text"
    assert out["totals"] == {"count": 9, "sum": 7044.3, "inflow": 10000.0, "outflow": -2955.7}
    assert out["by_category"] == [
        {"category": "Receita", "total": 10000.0, "count": 2},
        {"category": "Moradia", "total": -1800.0, "count": 1},
        {"category": "Alimentação", "total": -860.2, "count": 3},
        {"category": "Transporte", "total": -245.5, "count": 2},
        {"category": "(sem categoria)", "total": -50.0, "count": 1},
    ]
    assert out["by_month"] == [
        {"month": "2026-08", "total": 4604.5, "inflow": 5000.0, "outflow": -395.5, "count": 3},
        {"month": "2026-09", "total": 2439.8, "inflow": 5000.0, "outflow": -2560.2, "count": 6},
    ]
    assert out["mom"] == [{"month": "2026-09", "previous": "2026-08", "total": 2439.8, "delta": -2164.7,
                           "delta_pct": -47.0, "gap": False}]
    food = next(c for c in out["by_category_month"]["categories"] if c["category"] == "Alimentação")
    assert food["totals"] == {"2026-08": -350.0, "2026-09": -510.2}
    assert [t["row"] for t in out["top"]] == [2, 6, 8]
    assert out["top"][2] == {"row": 8, "date": "20/09/2026", "description": "Aluguel Setembro",
                             "category": "Moradia", "amount": -1800.0}


def test_analyze_debit_credit_columns_and_export(tmp_path: Path):
    src = tmp_path / "extrato.csv"
    src.write_text("Data,Histórico,Débito,Crédito\n2026-09-01,Compra,\"1,234.50\",\n"
                   "2026-09-02,Depósito,,2000\n2026-10-01,Tarifa,10.00,\n", encoding="utf-8")
    export = tmp_path / "norm.csv"
    out = analyze(str(src), "--engine", "python", "--export-csv", str(export))
    assert out["detected"]["amount"] is None and out["detected"]["debit"] == "Débito"
    assert out["totals"] == {"count": 3, "sum": 755.5, "inflow": 2000.0, "outflow": -1244.5}
    assert [m["month"] for m in out["by_month"]] == ["2026-09", "2026-10"]
    with export.open(encoding="utf-8") as fh:
        assert next(csv.reader(fh)) == ["Data", "Histórico", "Débito", "Crédito"]


BANK_EXPORT = ("Extrato de Conta Corrente\nAgência: 1234 Conta: 56789-0\n\n"
               "Data;Histórico;Tipo;Valor;categoria\n"
               "01/09/2026;Supermercado Extra;Débito;-1.120,50;Alimentação\n"
               "02/09/2026;Salário ACME;PIX;5.000,00;Receita\n"
               "03/09/2026;Uber *Trip;Débito;-30,00;Transporte\n")


def test_analyze_skips_bank_preamble_and_ignores_payment_type(tmp_path: Path):
    src = tmp_path / "extrato.csv"
    src.write_text(BANK_EXPORT, encoding="utf-8")
    out = analyze(str(src), "--engine", "python")
    assert out["row_count"] == 3 and [c["name"] for c in out["columns"]][:3] == ["Data", "Histórico", "Tipo"]
    assert out["detected"]["category"] == "categoria" and out["detected"]["amount"] == "Valor"
    assert out["totals"] == {"count": 3, "sum": 3849.5, "inflow": 5000.0, "outflow": -1150.5}
    assert [c["category"] for c in out["by_category"]] == ["Receita", "Alimentação", "Transporte"]
    assert any("preâmbulo" in w and "linha 4" in w for w in out["warnings"])
    explicit = analyze(str(src), "--engine", "python", "--skip-rows", "3")
    assert explicit["totals"] == out["totals"] and explicit["warnings"] == []
    bad = run([PY, str(ANALYZE), str(src), "--engine", "python", "--skip-rows", "-1"])
    assert bad.returncode == 2


def test_analyze_keeps_first_row_header_when_data_rows_are_wider(tmp_path: Path):
    """Ragged data (unquoted extra cells) must not be mistaken for a preamble."""
    src = tmp_path / "ragged.csv"
    src.write_text("data;valor\n2026-09-01;-10;extra\n2026-09-02;-5;extra\n", encoding="utf-8")
    out = analyze(str(src), "--engine", "python")
    assert out["row_count"] == 2 and out["totals"]["sum"] == -15.0 and out["warnings"] == []


def test_analyze_category_prefers_categoria_over_weaker_synonyms(tmp_path: Path):
    src = tmp_path / "x.csv"
    src.write_text("Data,Tipo,Grupo,Categoria,Valor\n2026-09-01,PIX,Casa,Moradia,-10\n", encoding="utf-8")
    assert analyze(str(src), "--engine", "python")["detected"]["category"] == "Categoria"
    src.write_text("Data,Tipo,Grupo,Valor\n2026-09-01,PIX,Casa,-10\n", encoding="utf-8")
    assert analyze(str(src), "--engine", "python")["detected"]["category"] == "Grupo"
    src.write_text("Data,Tipo,Valor\n2026-09-01,PIX,-10\n", encoding="utf-8")
    assert analyze(str(src), "--engine", "python")["detected"]["category"] is None


@pytest.mark.skipif(not shutil.which("duckdb"), reason="duckdb CLI not on PATH")
def test_analyze_duckdb_engine_matches_python_on_bank_preamble(tmp_path: Path):
    src = tmp_path / "extrato.csv"
    src.write_text(BANK_EXPORT, encoding="utf-8")
    py_out = analyze(str(src), "--engine", "python")
    duck_out = analyze(str(src), "--engine", "duckdb")
    for key in ("row_count", "detected", "totals", "by_category", "by_month", "top"):
        assert duck_out[key] == py_out[key], key
    assert analyze(str(src), "--engine", "duckdb", "--skip-rows", "3")["totals"] == py_out["totals"]


def _write_xlsx(path: Path, preamble: bool = False) -> None:
    """Minimal two-sheet XLSX: shared strings, inline string, Excel serial dates; optional title rows."""
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    rel_ns = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    shared = ["Data", "Descrição", "Categoria", "Valor", "Mercado", "Alimentação", "Salário", "Receita"]

    def cell(ref: str, value, kind: str = "n") -> str:
        if kind == "s":
            return f'<c r="{ref}" t="s"><v>{shared.index(value)}</v></c>'
        if kind == "inline":
            return f'<c r="{ref}" t="inlineStr"><is><t>{value}</t></is></c>'
        return f'<c r="{ref}"><v>{value}</v></c>'

    rows = [
        [cell("A1", "Data", "s"), cell("B1", "Descrição", "s"), cell("C1", "Categoria", "s"), cell("D1", "Valor", "s")],
        [cell("A2", 46266), cell("B2", "Mercado", "s"), cell("C2", "Alimentação", "s"), cell("D2", -100.25)],
        [cell("A3", 46297), cell("B3", "Salário", "s"), cell("C3", "Receita", "s"), cell("D3", 3000)],
        [cell("A4", 46298), cell("B4", "Feira livre", "inline"), cell("C4", "Alimentação", "s"), cell("D4", -50)],
    ]
    offset = 0
    if preamble:  # title in A1, row 2 absent from the XML (blank), table from row 3
        rows = [[c.replace(f'r="{c[6]}{n}"', f'r="{c[6]}{n + 2}"') for c in r]
                for n, r in enumerate(rows, 1)]
        offset = 2
    sheet1 = f'<worksheet {ns}><sheetData>' + (
        '<row r="1"><c r="A1" t="inlineStr"><is><t>Relatório de gastos</t></is></c></row>' if preamble else "") + "".join(
        f'<row r="{i + offset}">{"".join(r)}</row>' for i, r in enumerate(rows, 1)) + "</sheetData></worksheet>"
    sheet2 = f'<worksheet {ns}><sheetData><row r="1"><c r="B1" t="inlineStr"><is><t>valor</t></is></c></row>' \
             f'<row r="2"><c r="B2"><v>7</v></c></row></sheetData></worksheet>'
    sst = f'<sst {ns}>' + "".join(f"<si><t>{s}</t></si>" for s in shared) + "</sst>"
    workbook = (f'<workbook {ns} {rel_ns}><sheets><sheet name="Gastos" sheetId="1" r:id="rId1"/>'
                f'<sheet name="Outra" sheetId="2" r:id="rId2"/></sheets></workbook>')
    rels = ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="worksheet" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Type="worksheet" Target="/xl/worksheets/sheet2.xml"/></Relationships>')
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("xl/workbook.xml", workbook)
        zf.writestr("xl/_rels/workbook.xml.rels", rels)
        zf.writestr("xl/sharedStrings.xml", sst)
        zf.writestr("xl/worksheets/sheet1.xml", sheet1)
        zf.writestr("xl/worksheets/sheet2.xml", sheet2)


def test_analyze_xlsx_with_stdlib_reader(tmp_path: Path):
    book = tmp_path / "gastos.xlsx"
    _write_xlsx(book)
    out = analyze(str(book), "--engine", "python")
    assert out["row_count"] == 3
    assert out["detected"]["date"] == "Data" and out["detected"]["category"] == "Categoria"
    assert out["totals"] == {"count": 3, "sum": 2849.75, "inflow": 3000.0, "outflow": -150.25}
    assert [(m["month"], m["total"]) for m in out["by_month"]] == [("2026-09", -100.25), ("2026-10", 2950.0)]
    assert out["top"][2]["description"] == "Feira livre"
    other = analyze(str(book), "--engine", "python", "--sheet", "Outra")
    assert other["row_count"] == 1 and other["columns"][1]["name"] == "valor"
    bad = run([PY, str(ANALYZE), str(book), "--engine", "python", "--sheet", "Nope"])
    assert bad.returncode == 2 and "Gastos, Outra" in bad.stderr


def test_analyze_xlsx_title_rows_are_skipped(tmp_path: Path):
    book = tmp_path / "titulo.xlsx"
    _write_xlsx(book, preamble=True)
    out = analyze(str(book), "--engine", "python")
    assert out["row_count"] == 3 and out["detected"]["category"] == "Categoria"
    assert out["totals"]["sum"] == 2849.75 and any("preâmbulo" in w for w in out["warnings"])
    explicit = analyze(str(book), "--engine", "python", "--skip-rows", "2")
    assert explicit["totals"] == out["totals"] and explicit["warnings"] == []


@pytest.mark.skipif(not shutil.which("duckdb"), reason="duckdb CLI not on PATH")
def test_analyze_duckdb_engine_matches_python():
    py_out = analyze(str(FIXTURES / "transactions_br.csv"), "--engine", "python")
    duck_out = analyze(str(FIXTURES / "transactions_br.csv"), "--engine", "duckdb")
    assert duck_out.pop("engine") == "duckdb" and py_out.pop("engine") == "python"
    assert duck_out == py_out


def test_analyze_falls_back_when_duckdb_fails(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "duckdb"
    fake.write_text("#!/bin/sh\necho 'IO Error: extension excel unavailable' >&2\nexit 1\n", encoding="utf-8")
    fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"}
    out = run_json([PY, str(ANALYZE), str(FIXTURES / "transactions_br.csv")], env=env)
    assert out["engine"] == "python" and out["totals"]["sum"] == 7044.3
    assert any("duckdb falhou" in w and "extension excel" in w for w in out["warnings"])
    forced = run([PY, str(ANALYZE), str(FIXTURES / "transactions_br.csv"), "--engine", "duckdb"], env=env)
    assert forced.returncode == 2


def test_analyze_input_errors(tmp_path: Path):
    missing = run([PY, str(ANALYZE), str(tmp_path / "nope.csv")])
    assert missing.returncode == 2 and "não encontrado" in json.loads(missing.stderr)["error"]
    no_amount = tmp_path / "texto.csv"
    no_amount.write_text("nome,cidade\nAna,SP\n", encoding="utf-8")
    out = analyze(str(no_amount), "--engine", "python")
    assert "totals" not in out and any("--amount-col" in w for w in out["warnings"])
    wrong = run([PY, str(ANALYZE), str(no_amount), "--engine", "python", "--amount-col", "valor"])
    assert wrong.returncode == 2


def test_analyze_cp1252_semicolon(tmp_path: Path):
    src = tmp_path / "banco.csv"
    src.write_bytes("Data;Descrição;Valor\n01/09/2026;Café;-12,50\n".encode("cp1252"))
    out = analyze(str(src), "--engine", "python")
    assert out["totals"]["sum"] == -12.5 and out["detected"]["description"] == "Descrição"
    assert any("cp1252" in w for w in out["warnings"])


# ----------------------------------------------------------------------------- budget_check.py

def test_budget_check_against_analysis(tmp_path: Path):
    analysis = tmp_path / "analysis.json"
    analysis.write_text(json.dumps(analyze(str(FIXTURES / "transactions_br.csv"), "--engine", "python")))
    out = run_json([PY, str(BUDGET), str(analysis), "--budget", str(FIXTURES / "budget.yaml")])
    assert out["month"] == "2026-09" and out["expense_sign"] == "negative"
    rows = {r["category"]: r for r in out["categories"]}
    assert rows["Alimentação"] == {"category": "Alimentação", "budget": 500.0, "spent": 510.2, "remaining": -10.2,
                                   "used_pct": 102.0, "status": "over"}
    assert rows["Moradia"]["status"] == "warn" and rows["Moradia"]["used_pct"] == 90.0
    assert rows["Transporte"]["status"] == "ok" and rows["Lazer & Cultura"]["spent"] == 0.0
    assert [r["category"] for r in out["categories"]] == ["Alimentação", "Moradia", "Transporte", "Lazer & Cultura"]
    assert out["unbudgeted"] == [{"category": "(sem categoria)", "spent": 50.0}]
    assert out["unused_budget"] == ["Lazer & Cultura"]
    assert out["totals"] == {"budget": 3000.0, "spent_budgeted": 2510.2, "spent_unbudgeted": 50.0, "remaining": 489.8}
    assert out["counts"] == {"over": 1, "warn": 1, "ok": 2}


def test_budget_check_stdin_month_and_positive_sign(tmp_path: Path):
    src = tmp_path / "despesas.csv"
    src.write_text("data,categoria,valor\n2026-08-03,Mercado,80\n2026-09-03,Mercado,120\n", encoding="utf-8")
    analysis = json.dumps(analyze(str(src), "--engine", "python"))
    budget = tmp_path / "orc.json"
    budget.write_text(json.dumps({"Mercado": 100}), encoding="utf-8")
    out = run_json([PY, str(BUDGET), "-", "--budget", str(budget), "--month", "2026-08"], stdin=analysis)
    assert out["expense_sign"] == "positive"
    assert out["categories"][0] == {"category": "Mercado", "budget": 100.0, "spent": 80.0, "remaining": 20.0,
                                    "used_pct": 80.0, "status": "warn"}
    missing = run([PY, str(BUDGET), "-", "--budget", str(budget), "--month", "2026-01"], stdin=analysis)
    assert missing.returncode == 2 and "ausente" in missing.stderr


def test_budget_limits_follow_brazilian_number_format(tmp_path: Path):
    analysis = json.dumps(analyze(str(FIXTURES / "transactions_br.csv"), "--engine", "python"))
    budget = tmp_path / "orcamento.yaml"
    budget.write_text("Alimentação: 1.500\nMoradia: R$ 2.000,00  # aluguel\nTransporte: 300,50\n"
                      "\"Lazer & Cultura\": 1500.5\n", encoding="utf-8")
    out = run_json([PY, str(BUDGET), "-", "--budget", str(budget)], stdin=analysis)
    limits = {r["category"]: r["budget"] for r in out["categories"]}
    assert limits == {"Alimentação": 1500.0, "Moradia": 2000.0, "Transporte": 300.5, "Lazer & Cultura": 1500.5}
    assert {r["category"]: r["status"] for r in out["categories"]}["Alimentação"] == "ok"   # 510,20 of 1.500
    budget.write_text("Alimentação: 1,500\n", encoding="utf-8")
    ambiguous = run([PY, str(BUDGET), "-", "--budget", str(budget)], stdin=analysis)
    assert ambiguous.returncode == 2 and "ambíguo" in ambiguous.stderr
    budget.write_text("Alimentação: muito\n", encoding="utf-8")
    assert run([PY, str(BUDGET), "-", "--budget", str(budget)], stdin=analysis).returncode == 2


def test_budget_check_requires_category_months(tmp_path: Path):
    src = tmp_path / "sem_categoria.csv"
    src.write_text("data,valor\n2026-09-01,-10\n", encoding="utf-8")
    proc = run([PY, str(BUDGET), "-", "--budget", str(FIXTURES / "budget.yaml")],
               stdin=json.dumps(analyze(str(src), "--engine", "python")))
    assert proc.returncode == 2 and "by_category_month" in proc.stderr


# ----------------------------------------------------------------------------- categorize.py

def test_categorize_rules_longest_match_and_unknown_groups(tmp_path: Path):
    out_csv = tmp_path / "out.csv"
    out = run_json([PY, str(CATEGORIZE), str(FIXTURES / "expenses.csv"), "--rules", str(FIXTURES / "rules.yaml"),
                    "--output-csv", str(out_csv), "--rows"])
    assert out["fields"] == ["description"] and out["amount_column"] == "amount"
    assert (out["total_rows"], out["categorized"], out["unknown"], out["coverage_pct"]) == (8, 5, 3, 62.5)
    cats = {r["text"]: r["category"] for r in out["rows"]}
    assert cats["UBER EATS *PEDIDO 991"] == "Alimentação"       # longest match beats "uber"
    assert cats["Uber *Trip"] == "Transporte"
    assert cats["Uberlandia Hotel"] is None                     # whole-word keywords only
    assert cats["Salário ACME"] == "Receitas"                    # accent-insensitive regex rule
    assert out["by_category"][0] == {"category": "Alimentação", "count": 2, "total": -155.5}
    assert out["unknown_groups"][0] == {"key": "pix enviado maria", "count": 2, "total": -100.0,
                                        "examples": ["PIX ENVIADO 111 MARIA", "PIX ENVIADO 222 MARIA"], "rows": [6, 7]}
    with out_csv.open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert rows[1]["categoria"] == "Alimentação" and rows[1]["regra"] == "uber eats"


def test_categorize_brazilian_csv_and_json_input(tmp_path: Path):
    out = run_json([PY, str(CATEGORIZE), str(FIXTURES / "transactions_br.csv"),
                    "--rules", str(FIXTURES / "rules.yaml")])
    assert out["fields"] == ["Descrição"] and out["unknown"] == 1
    assert {c["category"]: c["total"] for c in out["by_category"]}["Alimentação"] == -860.2
    tx = tmp_path / "tx.json"
    tx.write_text(json.dumps({"transactions": [{"memo": "Posto Ipiranga", "valor": "-90,00"},
                                               {"memo": "Livraria", "valor": "-45"}]}), encoding="utf-8")
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps({"categories": {"Transporte": ["posto"]}}), encoding="utf-8")
    out = run_json([PY, str(CATEGORIZE), str(tx), "--rules", str(rules)])
    assert out["by_category"] == [{"category": "Transporte", "count": 1, "total": -90.0}]
    assert out["unknown_groups"][0]["key"] == "livraria"


@pytest.mark.parametrize("amounts, decimal", [
    (['"-1,234"', "-3.50", '"2,000.00"'], "dot"),            # US thousands + decimals
    (['"-1.234"', '"-3,50"', '"2.000,00"'], "comma"),        # BR
    (["-1.234", "-30", "-1.000"], "comma"),                   # BR thousands only
], ids=["us", "br", "br-thousands"])
def test_categorize_and_analyze_agree_on_amounts(tmp_path: Path, amounts: list[str], decimal: str):
    src = tmp_path / "tx.csv"
    src.write_text("date,description,amount\n" + "".join(
        f"2026-09-0{i},Loja {i},{a}\n" for i, a in enumerate(amounts, 1)), encoding="utf-8")
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps({"categories": {"Casa": ["loja 1"]}}), encoding="utf-8")
    cat = run_json([PY, str(CATEGORIZE), str(src), "--rules", str(rules)])
    total = sum(c["total"] for c in cat["by_category"]) + sum(g["total"] for g in cat["unknown_groups"])
    assert cat["amount_decimal"] == decimal
    assert round(total, 2) == analyze(str(src), "--engine", "python")["totals"]["sum"]
    forced = run_json([PY, str(CATEGORIZE), str(src), "--rules", str(rules), "--decimal", "comma"])
    assert forced["amount_decimal"] == "comma"


def test_categorize_rejects_bad_rules(tmp_path: Path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("categorias:\n  X: [a]\n", encoding="utf-8")
    proc = run([PY, str(CATEGORIZE), str(FIXTURES / "expenses.csv"), "--rules", str(bad)])
    assert proc.returncode == 2 and "categories" in proc.stderr
    bad.write_text('categories:\n  X: ["re:(unclosed"]\n', encoding="utf-8")
    proc = run([PY, str(CATEGORIZE), str(FIXTURES / "expenses.csv"), "--rules", str(bad)])
    assert proc.returncode == 2 and "regex" in proc.stderr


def _load_categorize():
    spec = importlib.util.spec_from_file_location("categorize_under_test", CATEGORIZE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("path", [RULES_TEMPLATE, FIXTURES / "rules.yaml"], ids=lambda p: p.name)
def test_builtin_yaml_parser_matches_pyyaml(path: Path):
    """categorize.py must work in a sandbox without PyYAML: its subset parser == PyYAML on our files."""
    text = path.read_text(encoding="utf-8")
    assert _load_categorize().parse_simple_yaml(text) == yaml.safe_load(text)


def test_rules_template_categorizes_common_merchants(tmp_path: Path):
    tx = tmp_path / "tx.csv"
    tx.write_text("descricao;valor\nNETFLIX.COM;-55,90\nDROGASIL 123;-30\nPIX RECEBIDO FULANO;100\n", encoding="utf-8")
    out = run_json([PY, str(CATEGORIZE), str(tx), "--rules", str(RULES_TEMPLATE), "--rows"])
    assert [r["category"] for r in out["rows"]] == ["Assinaturas", "Saúde", "Receitas"]
