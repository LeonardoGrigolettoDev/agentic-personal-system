"""Cálculo de ICMS das vendas interestaduais do mini-ERP (versão simplificada para estudo)."""

from decimal import ROUND_HALF_UP, Decimal


def icms_sp(valor: Decimal, frete: Decimal, desconto: Decimal) -> dict:
    base = valor + frete - desconto
    if base < 0:
        raise ValueError("base negativa")
    aliquota = Decimal("0.18")
    imposto = (base * aliquota).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    fcp = Decimal("0")
    total = base + imposto + fcp
    return {"uf": "SP", "base": base, "aliquota": aliquota, "icms": imposto, "fcp": fcp, "total": total}


def icms_rj(valor: Decimal, frete: Decimal, desconto: Decimal) -> dict:
    base = valor + frete - desconto
    if base < 0:
        raise ValueError("base negativa")
    aliquota = Decimal("0.20")
    imposto = (base * aliquota).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    fcp = (base * Decimal("0.02")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    total = base + imposto + fcp
    return {"uf": "RJ", "base": base, "aliquota": aliquota, "icms": imposto, "fcp": fcp, "total": total}


def icms_mg(valor: Decimal, frete: Decimal, desconto: Decimal) -> dict:
    base = valor + frete - desconto
    if base < 0:
        raise ValueError("base negativa")
    aliquota = Decimal("0.18")
    imposto = (base * aliquota).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    fcp = (base * Decimal("0.02")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    total = base + imposto + fcp
    return {"uf": "MG", "base": base, "aliquota": aliquota, "icms": imposto, "fcp": fcp, "total": total}


def icms_pr(valor: Decimal, frete: Decimal, desconto: Decimal) -> dict:
    base = valor + frete - desconto
    if base < 0:
        raise ValueError("base negativa")
    aliquota = Decimal("0.195")
    imposto = (base * aliquota).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    fcp = Decimal("0")
    total = base + imposto + fcp
    return {"uf": "PR", "base": base, "aliquota": aliquota, "icms": imposto, "fcp": fcp, "total": total}


def calcula_icms(uf: str, valor: Decimal, frete: Decimal = Decimal("0"), desconto: Decimal = Decimal("0")) -> dict:
    uf = uf.upper()
    if uf == "SP":
        return icms_sp(valor, frete, desconto)
    elif uf == "RJ":
        return icms_rj(valor, frete, desconto)
    elif uf == "MG":
        return icms_mg(valor, frete, desconto)
    elif uf == "PR":
        return icms_pr(valor, frete, desconto)
    else:
        raise ValueError(f"UF não suportada: {uf}")
