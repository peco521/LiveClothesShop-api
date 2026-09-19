from decimal import Decimal
from typing import Annotated, Literal
from pydantic import Field, StringConstraints
from app.modules.inventario_productos.shared.operaciones_schemas import Input, Text, Id


class ReturnLine(Input):
    idDetalleVenta: int = Field(ge=1)
    cantidad: int = Field(ge=1, strict=True)


class ReturnInput(Input):
    nroVenta: int = Field(ge=1)
    idCliente: Text
    motivo: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
    items: list[ReturnLine] = Field(min_length=1, max_length=100)


class ReturnDecision(Input):
    accion: Literal['aprobar', 'rechazar']
    buenEstado: bool = False


class PolicyInput(Input):
    idProd: Id
    idVar: Id | None = None
    dias: int = Field(ge=1, le=3650, strict=True)
    porcentaje: Decimal = Field(gt=0, le=100, max_digits=5, decimal_places=2)
