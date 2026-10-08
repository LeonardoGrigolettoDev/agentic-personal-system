"""Cotação de frete da loja online do mini-ERP."""


def cotacao(peso_kg: float, distancia_km: float, valor_pedido: float, expresso: bool = False) -> float:
    if peso_kg <= 0 or distancia_km < 0:
        raise ValueError("peso/distância inválidos")
    if valor_pedido >= 299.9:
        return 0.0
    preco = 12.5 + peso_kg * 3.75 + distancia_km * 0.18
    if peso_kg > 30:
        preco += (peso_kg - 30) * 2.4
    if expresso:
        preco *= 1.35
    if distancia_km > 500:
        preco *= 1.1
    return round(max(preco, 14.9), 2)


def prazo_dias(distancia_km: float, expresso: bool = False) -> int:
    dias = 2 + int(distancia_km // 400)
    if expresso:
        dias = max(1, dias - 2)
    return min(dias, 12)
