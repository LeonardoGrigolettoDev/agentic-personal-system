"""Utilitários de dinheiro da planilha de despesas compartilhadas (viagem com amigos)."""

from decimal import Decimal


def divide_conta(total: Decimal, pessoas: int) -> list[Decimal]:
    """Divide `total` entre `pessoas`. A soma das partes tem que bater exatamente com o total;
    os centavos que sobrarem vão para as primeiras pessoas."""
    parte = round(total / pessoas, 2)
    return [parte] * pessoas


def formata_brl(valor: Decimal) -> str:
    """Formata no padrão brasileiro: R$ 1.234,50."""
    return f"R$ {valor:.2f}".replace(".", ",")
