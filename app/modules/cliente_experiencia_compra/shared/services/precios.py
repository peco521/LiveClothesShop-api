"""Reglas de precio/promoción compartidas (CU10, CU12 y CU13 usan las mismas)."""

from datetime import date
from decimal import Decimal, ROUND_HALF_UP


def promocion_vigente(promo) -> bool:
    """Una promoción solo se expone si está activa y dentro de fechas."""
    if promo is None or promo.estado != "activo":
        return False
    today = date.today()
    if promo.fechaini and today < promo.fechaini:
        return False
    if promo.fechafin and today > promo.fechafin:
        return False
    return True


def moneda(value) -> Decimal:
    """Redondeo monetario consistente a 2 decimales."""
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def descuento_unitario(precio, promo) -> Decimal:
    """Descuento por unidad de una promoción vigente (0 si no aplica).

    Semántica montoFijo: por unidad (igual que porcentaje), porque el schema
    (tipoDescuento/valorDescuento sin ámbito) y la documentación no definen
    otro ámbito. Nunca supera el precio base.
    """
    base = moneda(precio)
    if not promocion_vigente(promo):
        return Decimal("0")
    if promo.tipodescuento == "porcentaje":
        descuento = base * Decimal(str(promo.valordescuento)) / Decimal("100")
    else:
        descuento = Decimal(str(promo.valordescuento))
    return min(moneda(descuento), base)
