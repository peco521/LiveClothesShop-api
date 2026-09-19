from datetime import date
from decimal import Decimal
from typing import Annotated, Literal
from pydantic import Field, StringConstraints, model_validator
from app.modules.inventario_productos.shared.operaciones_schemas import Input, Text, Id


class PromotionInput(Input):
    nombre: Text
    descripcion: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
    tipoDescuento: Literal['porcentaje', 'montoFijo']
    valorDescuento: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    fechaIni: date
    fechaFin: date
    productos: list[Id] = Field(min_length=1, max_length=500)

    @model_validator(mode='after')
    def validate_values(self):
        if self.fechaFin <= self.fechaIni:
            raise ValueError('La fecha final debe ser posterior a la inicial')
        if self.tipoDescuento == 'porcentaje' and self.valorDescuento > 100:
            raise ValueError('El porcentaje no puede superar 100')
        if len(set(self.productos)) != len(self.productos):
            raise ValueError('No repitas productos')
        return self
