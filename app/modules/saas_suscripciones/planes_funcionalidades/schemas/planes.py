from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator


class PlanDatos(BaseModel):
    model_config = ConfigDict(extra='forbid')

    nombre: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=30)]
    maxSucursales: int = Field(strict=True, ge=1, le=32767)
    estado: Literal['activo', 'inactivo'] = 'activo'
    funcionalidades: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=30)]] = Field(max_length=100)

    @field_validator('funcionalidades')
    @classmethod
    def codigos_unicos(cls, values):
        if len(values) != len(set(values)):
            raise ValueError('Las funcionalidades no pueden repetirse')
        return sorted(values)


class PlanActualizacion(PlanDatos):
    confirmacionImpacto: str | None = Field(default=None, min_length=64, max_length=64)
    suscripcionesSeleccionadas: list[Annotated[int, Field(strict=True, gt=0)]] = Field(default_factory=list, max_length=10000)

    @field_validator('suscripcionesSeleccionadas')
    @classmethod
    def suscripciones_unicas(cls, values):
        if len(values) != len(set(values)):
            raise ValueError('Las suscripciones no pueden repetirse')
        return sorted(values)
