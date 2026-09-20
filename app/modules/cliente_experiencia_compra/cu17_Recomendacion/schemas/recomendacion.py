"""Contrato HTTP de CU17.

``RecomendacionItem`` extiende la tarjeta de producto de CU10 en lugar de crear
una representación paralela del mismo producto. La tienda vende únicamente
poleras, por lo que ``categoria`` debe leerse como el MODELO/ESTILO de polera
(Deportiva, Semi-Formal, ...), nunca como un tipo de prenda distinto.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.modules.cliente_experiencia_compra.cu10_consultar_prendas.schemas.catalogo import (
    ProductoResumen,
)

TipoRecomendacion = Literal["personalizada", "general"]


class RecomendacionItem(ProductoResumen):
    """Tarjeta de CU10 + explicabilidad. No se exponen porcentajes inventados."""

    model_config = ConfigDict(extra="forbid")

    score: int
    razones: list[str] = []


class RecomendacionesRespuesta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tipo: TipoRecomendacion
    mensaje: str
    total: int
    items: list[RecomendacionItem] = []
