from decimal import Decimal
from uuid import UUID
from typing import Annotated, Literal
from pydantic import Field, StringConstraints
from app.modules.inventario_productos.shared.operaciones_schemas import Input, Text, Id


class SaleLine(Input):
    idVar: Id
    cantidad: int = Field(ge=1, strict=True)


class SaleInput(Input):
    claveOperacion: UUID
    nroSuc: int = Field(ge=1)
    # CU24: la venta anónima no identifica cliente. Una venta de reserva sí lo exige.
    idCliente: Text | None = None
    nit: Annotated[str, StringConstraints(strip_whitespace=True, max_length=30)] | None = None
    nroReserva: int | None = Field(None, ge=1)
    items: list[SaleLine] = Field(min_length=1, max_length=100)


class CashInput(Input):
    recibido: Decimal = Field(ge=0, max_digits=10, decimal_places=2)


class ElectronicInput(Input):
    metodo: Literal['tarjeta', 'QR', 'transferencia']
