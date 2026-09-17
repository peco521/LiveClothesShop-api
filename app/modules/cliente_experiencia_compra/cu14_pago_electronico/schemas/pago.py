from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

MetodoPago = Literal["tarjeta", "QR", "transferencia"]
EscenarioMock = Literal["aprobado", "rechazado", "timeout"]
EstadoPago = Literal["pendiente", "aprobado", "rechazado"]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PagoCrear(Input):
    nroVenta: int
    metodo: MetodoPago
    escenario: EscenarioMock | None = None


class PagoReprocesar(Input):
    escenario: EscenarioMock | None = None


class PagoDetalle(BaseModel):
    idPago: int
    metodo: str
    monto: Decimal
    estado: EstadoPago
    fechaHora: datetime
    referencia: str | None
    nroVenta: int
    estadoVenta: str
    checkoutUrl: str | None = None
