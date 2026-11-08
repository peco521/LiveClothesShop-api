"""Allowlisted query plans: no SQL, expressions, tenant or arbitrary grouping."""
from datetime import date
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

ReportKind = Literal['ventas_sucursal', 'productos_vendidos', 'ventas_semana', 'existencias_bajas']

class ReportPlan(BaseModel):
    model_config = ConfigDict(extra='forbid')
    tipo: ReportKind
    fechaIni: date | None = None
    fechaFin: date | None = None
    nroSuc: int | None = Field(default=None, ge=1, strict=True)
    limite: int = Field(default=10, ge=1, le=100, strict=True)
    umbral: int = Field(default=5, ge=0, le=100000, strict=True)

    @model_validator(mode='after')
    def valid_range(self):
        if self.tipo != 'existencias_bajas' and (self.fechaIni is None or self.fechaFin is None):
            raise ValueError('Indica ambas fechas para ventas')
        if (self.fechaIni is None) != (self.fechaFin is None):
            raise ValueError('Indica ambas fechas o ninguna')
        if self.fechaIni is not None and (self.fechaFin < self.fechaIni or (self.fechaFin-self.fechaIni).days > 366):
            raise ValueError('El período debe ser ordenado y de hasta 367 días')
        return self

class InterpretRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    texto: str = Field(min_length=3, max_length=1200)
    canal: Literal['texto', 'voz'] = 'texto'

class ModelDecision(BaseModel):
    model_config = ConfigDict(extra='forbid')
    estado: Literal['listo', 'aclaracion', 'no_soportado']
    mensaje: str = Field(max_length=300)
    plan: ReportPlan | None = None

class InterpreterPlan(BaseModel):
    model_config = ConfigDict(extra='forbid')
    tipo: Literal['ventas_sucursal','productos_vendidos','ventas_semana','existencias_bajas','aclaracion','no_soportado']
    fechaIni: date | None
    fechaFin: date | None
    nroSuc: int | None
    limite: int
    umbral: int
    mensaje: str = Field(max_length=300)

class PredictionRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    category: Literal['camiseta','pantalon','vestido']
    day_of_week: int = Field(ge=0,le=6,strict=True)
    month: int = Field(ge=1,le=12,strict=True)
    price: float = Field(ge=1,le=500,allow_inf_nan=False)
    discount: float = Field(ge=0,le=0.7,allow_inf_nan=False)
    promotion: int = Field(ge=0,le=1,strict=True)

class StockParameters(BaseModel):
    model_config = ConfigDict(extra='forbid')
    stock_maximo: int | None = Field(ge=0,le=100000)
    cantidad_filas: int | None = Field(ge=1,le=100)
