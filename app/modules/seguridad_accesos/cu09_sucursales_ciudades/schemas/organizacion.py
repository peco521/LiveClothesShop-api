from typing import Annotated, Literal

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
    id: CityId
    nombre: Nombre


class CiudadEditar(Patch):
    nombre: Nombre | None = None


class SucursalCrear(Input):
    nombre: Nombre
    direccion: Direccion
    estado: Estado = "activo"
    idCiud: CityId


class SucursalEditar(Patch):
    nombre: Nombre | None = None
    direccion: Direccion | None = None
    estado: Estado | None = None
    idCiud: CityId | None = None


class CiudadDetalle(BaseModel):
    id: int
    nombre: str


class SucursalDetalle(BaseModel):
    nro: int
    nombre: str
    direccion: str
    estado: Estado
    idCiud: int
    ciudad: CiudadDetalle


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
