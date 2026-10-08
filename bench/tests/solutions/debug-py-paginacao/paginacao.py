"""Paginação da listagem de produtos do mini-ERP (API e tela de estoque)."""


def total_paginas(total_itens: int, tamanho: int = 20) -> int:
    """Quantidade de páginas necessárias para mostrar `total_itens` (0 itens -> 0 páginas)."""
    if tamanho <= 0:
        raise ValueError("tamanho deve ser positivo")
    return -(-total_itens // tamanho)


def pagina(itens: list, numero: int, tamanho: int = 20) -> list:
    """Itens da página `numero` (1-indexada). Página fora do intervalo -> lista vazia."""
    if numero < 1:
        raise ValueError("página começa em 1")
    inicio = (numero - 1) * tamanho
    return itens[inicio:inicio + tamanho]
