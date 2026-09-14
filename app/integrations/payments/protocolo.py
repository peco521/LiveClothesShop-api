"""Contrato de pasarela de pago electrónica (CU14).

El backend nunca ve datos financieros sensibles: solo recibe método y monto
(calculado del servidor) y devuelve estado + referencia no sensible.
"""

from decimal import Decimal
from typing import Literal, Protocol

MetodoPago = Literal["tarjeta", "QR", "transferencia"]
ResultadoPago = Literal["aprobado", "rechazado"]
EscenarioMock = Literal["aprobado", "rechazado", "timeout"]


class ResultadoPasarela:
    """Resultado de la pasarela: autorización de cobro, no entrega de stock."""

    def __init__(self, estado: ResultadoPago, referencia: str | None = None):
        self.estado = estado
        self.referencia = referencia


class TimeoutPasarela(Exception):
    """La pasarela no respondió a tiempo: resultado desconocido (no rechazar)."""


class PasarelaPagos(Protocol):
    def cobrar(self, *, monto: Decimal, metodo: str, nro_venta: int, id_pago: int,
               escenario: EscenarioMock | None = None) -> ResultadoPasarela:
        """Autoriza el cobro. Puede lanzar TimeoutPasarela si no hay certeza."""
        ...
