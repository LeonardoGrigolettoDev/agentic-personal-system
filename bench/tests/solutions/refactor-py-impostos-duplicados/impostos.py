"""Cálculo de ICMS das vendas interestaduais do mini-ERP (versão simplificada para estudo)."""

from decimal import ROUND_HALF_UP, Decimal

CENTAVO = Decimal("0.01")
# UF -> (alíquota de ICMS, alíquota do Fundo de Combate à Pobreza)
ALIQUOTAS = {
    "SP": (Decimal("0.18"), Decimal(0)),
    "RJ": (Decimal("0.20"), Decimal("0.02")),
    "MG": (Decimal("0.18"), Decimal("0.02")),
    "PR": (Decimal("0.195"), Decimal(0)),
}


def _arredonda(valor: Decimal) -> Decimal:
    return valor.quantize(CENTAVO, rounding=ROUND_HALF_UP)


def calcula_icms(uf: str, valor: Decimal, frete: Decimal = Decimal(0), desconto: Decimal = Decimal(0)) -> dict:
    uf = uf.upper()
    if uf not in ALIQUOTAS:
        raise ValueError(f"UF não suportada: {uf}")
    base = valor + frete - desconto
    if base < 0:
        raise ValueError("base negativa")
    aliquota, aliquota_fcp = ALIQUOTAS[uf]
    imposto = _arredonda(base * aliquota)
    fcp = _arredonda(base * aliquota_fcp) if aliquota_fcp else Decimal(0)
    return {"uf": uf, "base": base, "aliquota": aliquota, "icms": imposto, "fcp": fcp, "total": base + imposto + fcp}
