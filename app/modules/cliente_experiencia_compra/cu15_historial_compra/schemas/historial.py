from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

EstadoVenta = Literal["registrada", "anulada"]
EstadoPago = Literal["pendiente", "aprobado", "rechazado"]


class ProductoCompraResumen(BaseModel):
    idVar: str
    sku: str
    producto: str
    cantidad: int


class CompraResumen(BaseModel):
    nroVenta: int
    fechaHora: datetime
    productos: list[ProductoCompraResumen]
    monto: Decimal
    estado: EstadoVenta
    estadoPago: EstadoPago | None = None


class HistorialCompras(BaseModel):
    items: list[CompraResumen]
    total: int
    offset: int
    limit: int
    mensaje: str | None = None


class CompraItemDetalle(ProductoCompraResumen):
    idDetalleVenta: int
    precioUnitario: Decimal
    subtotalBruto: Decimal


class CompraSucursal(BaseModel):
    nro: int
    nombre: str
    ciudad: str


class CompraPago(BaseModel):
    idPago: int
    metodo: str
    monto: Decimal
    estado: EstadoPago
    fechaHora: datetime
    referencia: str | None


class CompraDetalle(BaseModel):
    nroVenta: int
    fechaHora: datetime
    estado: EstadoVenta
    estadoPago: EstadoPago | None = None
    nit: str | None
    sucursal: CompraSucursal
    idCarrito: int | None
    nroReserva: int | None
    items: list[CompraItemDetalle]
    brutoTotal: Decimal
    descAplicado: Decimal
    total: Decimal
    pago: CompraPago | None = None
