from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID
from pydantic import Field, StringConstraints
from app.modules.inventario_productos.shared.operaciones_schemas import Input, ProductId, Text, Id


class ReturnLine(Input):
    idDetalleVenta: int = Field(ge=1)
    cantidad: int = Field(ge=1, strict=True)


class ExchangeLine(ReturnLine):
    idVarNueva: Id


class ReturnInput(Input):
    idOperacion: UUID
    nroVenta: int = Field(ge=1)
    idCliente: Text
    motivo: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
    items: list[ReturnLine] = Field(min_length=1, max_length=100)


class ExchangeInput(ReturnInput):
    items: list[ExchangeLine] = Field(min_length=1, max_length=100)


class InspectionLine(Input):
    idDetalleVenta: int = Field(ge=1)
    condicion: Literal['apta', 'danada']


class ReturnDecision(Input):
    accion: Literal['inspeccionar', 'aprobar', 'rechazar']
    inspecciones: list[InspectionLine] = Field(default_factory=list, max_length=100)


class PolicyInput(Input):
    idProd: ProductId
    idVar: Id | None = None
    dias: int = Field(ge=1, le=3650, strict=True)
    porcentaje: Decimal = Field(gt=0, le=100, max_digits=5, decimal_places=2)
