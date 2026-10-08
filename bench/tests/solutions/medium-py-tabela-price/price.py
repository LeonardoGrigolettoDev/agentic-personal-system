"""Simulador de financiamento (tabela Price) da planilha de finanças pessoais."""

from decimal import ROUND_HALF_UP, Decimal

CENTAVO = Decimal("0.01")


def _cent(v: Decimal) -> Decimal:
    return v.quantize(CENTAVO, rounding=ROUND_HALF_UP)


def _valida(valor: Decimal, n: int, juros_mensal: Decimal) -> None:
    if valor <= 0 or n < 1 or juros_mensal < 0:
        raise ValueError("valor > 0, n >= 1 e juros >= 0")


def parcela_price(valor: Decimal, n: int, juros_mensal: Decimal) -> Decimal:
    _valida(valor, n, juros_mensal)
    if juros_mensal == 0:
        return _cent(valor / n)
    return _cent(valor * juros_mensal / (1 - (1 + juros_mensal) ** -n))


def tabela_price(valor: Decimal, n: int, juros_mensal: Decimal) -> list[dict]:
    parcela = parcela_price(valor, n, juros_mensal)
    saldo = _cent(valor)
    linhas = []
    for k in range(1, n + 1):
        juros = _cent(saldo * juros_mensal)
        amort = saldo if k == n else parcela - juros
        saldo = _cent(saldo - amort)
        linhas.append({"numero": k, "parcela": juros + amort, "juros": juros, "amortizacao": amort, "saldo": saldo})
    return linhas


def total_pago(valor: Decimal, n: int, juros_mensal: Decimal) -> Decimal:
    return sum((r["parcela"] for r in tabela_price(valor, n, juros_mensal)), Decimal(0))
