"""Utilitários de dinheiro da planilha de despesas compartilhadas (viagem com amigos)."""

from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

CENTAVO = Decimal("0.01")


def divide_conta(total: Decimal, pessoas: int) -> list[Decimal]:
    """Divide `total` entre `pessoas`. A soma das partes tem que bater exatamente com o total;
    os centavos que sobrarem vão para as primeiras pessoas."""
    base = (total / pessoas).quantize(CENTAVO, rounding=ROUND_DOWN)
    sobra = int((total - base * pessoas) / CENTAVO)
    return [base + CENTAVO if i < sobra else base for i in range(pessoas)]


def formata_brl(valor: Decimal) -> str:
    """Formata no padrão brasileiro: R$ 1.234,50."""
    texto = f"{valor.quantize(CENTAVO, rounding=ROUND_HALF_UP):,.2f}"
    return "R$ " + texto.replace(",", "_").replace(".", ",").replace("_", ".")
