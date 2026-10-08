"""Carrinho de compras da loja online do mini-ERP."""

from decimal import Decimal


class Carrinho:
    def __init__(self, cliente: str, itens: list | None = None):
        self.cliente = cliente
        self.itens = list(itens) if itens else []

    def adiciona(self, sku: str, preco: Decimal, quantidade: int = 1) -> None:
        self.itens.append((sku, Decimal(preco), quantidade))

    def total(self) -> Decimal:
        return sum((preco * qtd for _, preco, qtd in self.itens), Decimal(0))


def etiquetas(pedido_id: int, extras: list | None = None) -> list:
    """Etiquetas impressas na caixa: sempre o número do pedido + extras."""
    return [*(extras or []), f"PEDIDO-{pedido_id}"]
