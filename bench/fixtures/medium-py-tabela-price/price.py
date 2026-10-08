"""Simulador de financiamento (tabela Price) da planilha de finanças pessoais.

Regras:
- parcela_price: PMT = valor * i / (1 - (1 + i) ** -n), arredondada para centavos (meio centavo para cima);
  juros 0 -> valor / n arredondado.
- tabela_price: uma linha por mês {numero, parcela, juros, amortizacao, saldo}. Juros do mês = saldo * i
  arredondado; amortização = parcela - juros. Na ÚLTIMA linha a amortização é o saldo restante e a parcela é
  juros + amortização (absorve os centavos de arredondamento), deixando saldo 0.
- valor <= 0, n < 1 ou juros negativos -> ValueError.
Todos os valores monetários são Decimal.
"""

from decimal import Decimal


def parcela_price(valor: Decimal, n: int, juros_mensal: Decimal) -> Decimal:
    raise NotImplementedError


def tabela_price(valor: Decimal, n: int, juros_mensal: Decimal) -> list[dict]:
    raise NotImplementedError


def total_pago(valor: Decimal, n: int, juros_mensal: Decimal) -> Decimal:
    raise NotImplementedError
