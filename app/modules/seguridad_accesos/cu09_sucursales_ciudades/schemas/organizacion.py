from typing import Annotated, Literal
from datetime import time
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

CityId = Annotated[int, Field(strict=True, ge=-32768, le=32767)]
Nombre = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]
Direccion = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Estado = Literal["activo", "inactivo"]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Patch(Input):
    @model_validator(mode="after")
    def nonempty(self):
        if not self.model_fields_set or any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("Envíe campos válidos para modificar")
        return self


class CiudadCrear(Input):
    nombre: Nombre


class CiudadEditar(Patch):
    nombre: Nombre | None = None


class HorarioRango(Input):
    dias: list[Annotated[int, Field(strict=True, ge=1, le=7)]] = Field(default_factory=lambda: [1, 2, 3, 4, 5, 6, 7], min_length=1, max_length=7)
    horaIni: time
    horaFin: time

    @model_validator(mode="after")
    def validar(self):
        if self.horaIni.tzinfo or self.horaFin.tzinfo or self.horaIni >= self.horaFin:
            raise ValueError("La hora de cierre debe ser posterior a la apertura, sin zona horaria")
        if len(set(self.dias)) != len(self.dias):
            raise ValueError("No repita los días de atención")
        self.dias = sorted(self.dias)
        return self


class HorariosValidos(Input):
    horarios: list[HorarioRango] = Field(default_factory=list, max_length=14)

    @model_validator(mode="after")
    def sin_solapamientos(self):
        rangos = sorted(self.horarios, key=lambda rango: rango.horaIni)
        if any(set(a.dias) & set(b.dias) and a.horaFin >= b.horaIni
               for i, a in enumerate(rangos) for b in rangos[i + 1:]):
            raise ValueError("Los horarios no deben superponerse ni duplicarse")
        if len({(r.horaIni, r.horaFin) for r in rangos}) != len(rangos):
            raise ValueError("Seleccione todos los días del mismo rango en una sola fila")
        return self


class SucursalCrear(HorariosValidos):
    nombre: Nombre
    direccion: Direccion
    estado: Estado = "activo"
    idCiud: CityId


class SucursalEditar(Patch):
    horarios: list[HorarioRango] | None = Field(None, max_length=14)

    @model_validator(mode="after")
    def validar_horarios(self):
        if self.horarios is not None:
            HorariosValidos(horarios=self.horarios)
        return self
    nombre: Nombre | None = None
    direccion: Direccion | None = None
    estado: Estado | None = None
    idCiud: CityId | None = None


class CiudadDetalle(BaseModel):
    id: int
    nombre: str


class HorarioSugerencia(BaseModel):
    idAten: int
    horaIni: time
    horaFin: time


class SucursalDetalle(BaseModel):
    horarios: list[HorarioRango] = Field(default_factory=list)
    nro: int
    nombre: str
    direccion: str
    estado: Estado
    idCiud: int
    ciudad: CiudadDetalle
    # CU09: coordenadas verificadas por el backend. NULL en sucursales antiguas
    # o cuando la validación de direcciones está desactivada.
    latitud: Decimal | None = None
    longitud: Decimal | None = None


class CiudadesFiltros(Input):
    offset: int = Field(0, ge=0, le=9223372036854775807)
    limit: int = Field(20, ge=1, le=100)
    q: Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)] = ""


class SucursalesFiltros(CiudadesFiltros):
    idCiud: int | None = Field(None, ge=-32768, le=32767)
    estado: Estado | None = None


class CiudadesListado(BaseModel):
    items: list[CiudadDetalle]
    total: int
    offset: int
    limit: int


class SucursalesListado(BaseModel):
    items: list[SucursalDetalle]
    total: int
    offset: int
    limit: int
