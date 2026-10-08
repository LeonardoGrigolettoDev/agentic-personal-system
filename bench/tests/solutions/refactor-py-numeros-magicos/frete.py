"""Cotação de frete da loja online do mini-ERP."""

FRETE_GRATIS_A_PARTIR_DE = 299.9
TAXA_FIXA = 12.5
POR_KG = 3.75
POR_KM = 0.18
LIMITE_PESO_KG = 30
POR_KG_EXCEDENTE = 2.4
FATOR_EXPRESSO = 1.35
DISTANCIA_LONGA_KM = 500
FATOR_DISTANCIA_LONGA = 1.1
FRETE_MINIMO = 14.9

PRAZO_BASE_DIAS = 2
KM_POR_DIA = 400
DIAS_GANHOS_EXPRESSO = 2
PRAZO_MINIMO_DIAS = 1
PRAZO_MAXIMO_DIAS = 12


def cotacao(peso_kg: float, distancia_km: float, valor_pedido: float, expresso: bool = False) -> float:
    if peso_kg <= 0 or distancia_km < 0:
        raise ValueError("peso/distância inválidos")
    if valor_pedido >= FRETE_GRATIS_A_PARTIR_DE:
        return 0.0
    preco = TAXA_FIXA + peso_kg * POR_KG + distancia_km * POR_KM
    if peso_kg > LIMITE_PESO_KG:
        preco += (peso_kg - LIMITE_PESO_KG) * POR_KG_EXCEDENTE
    if expresso:
        preco *= FATOR_EXPRESSO
    if distancia_km > DISTANCIA_LONGA_KM:
        preco *= FATOR_DISTANCIA_LONGA
    return round(max(preco, FRETE_MINIMO), 2)


def prazo_dias(distancia_km: float, expresso: bool = False) -> int:
    dias = PRAZO_BASE_DIAS + int(distancia_km // KM_POR_DIA)
    if expresso:
        dias = max(PRAZO_MINIMO_DIAS, dias - DIAS_GANHOS_EXPRESSO)
    return min(dias, PRAZO_MAXIMO_DIAS)
