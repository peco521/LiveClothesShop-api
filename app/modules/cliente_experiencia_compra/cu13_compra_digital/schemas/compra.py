from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Nit = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=30)]
EstadoVenta = Literal["registrada", "anulada"]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CompraCrear(Input):
    nroSuc: int = Field(ge=1)
    nit: Nit | None = None


class VentaItem(BaseModel):
    idDetalleVenta: int
    idVar: str
    sku: str
    producto: str
    cantidad: int
    precioUnitario: Decimal
    subtotalBruto: Decimal


class VentaSucursal(BaseModel):
    nro: int
    nombre: str
    ciudad: str


class VentaDetalle(BaseModel):
    nroVenta: int
    fechaHora: datetime
    estado: EstadoVenta
    nit: str | None
    sucursal: VentaSucursal
    # Las ventas de carrito (CU13) siempre traen carrito; la venta presencial de
    # caja (CU24) se registra sin carrito, por eso es opcional.
    carrito: int | None
    items: list[VentaItem]
    brutoTotal: Decimal
    descAplicado: Decimal
    total: Decimal
