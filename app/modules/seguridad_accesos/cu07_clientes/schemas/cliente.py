from datetime import date
from typing import Annotated, Literal

from pydantic import AliasChoices, BaseModel, EmailStr, Field, StringConstraints, field_validator, model_validator

from app.modules.seguridad_accesos.schemas.auth import PublicInput, RolResponse, normalize_email

Text100 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Text50 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]


class ClienteEditar(PublicInput):
    ci: Text100 | None = None
    nombre: Text100 | None = Field(None, validation_alias=AliasChoices("nombres", "nombre"))
    apellidoPat: Text50 | None = None
    apellidoMat: Text50 | None = None
    sexo: Literal["M", "F"] | None = None
    correo: EmailStr | None = Field(default=None, max_length=100)
    telefono: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20)] | None = None
    direccion: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=150)] | None = None
    fechaNac: date | None = None

    _correo = field_validator("correo", mode="before")(normalize_email)

    @field_validator("fechaNac")
    @classmethod
    def valid_birth_date(cls, value):
        if value is not None and value > date.today():
            raise ValueError("Fecha inválida")
        return value

    @model_validator(mode="after")
    def nonempty_changes(self):
        if not self.model_fields_set or any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("Envíe campos válidos para modificar")
        return self


class ClientePerfil(BaseModel):
    cod_cl: str
    estado: Literal["frecuente", "casual", "inactivo"]


class ClienteDetalle(BaseModel):
    idUsuario: str
    ci: str
    nombre: str | None = Field(validation_alias=AliasChoices("nombre", "nombres"), serialization_alias="nombres")
    apellidoPat: str
    apellidoMat: str
    sexo: Literal["M", "F"]
    correo: str
    telefono: str
    direccion: str
    fechaNac: date
    tipo: Literal["C"]
    nroRol: str
    rol: RolResponse
    cliente: ClientePerfil


class ClientesListado(BaseModel):
    items: list[ClienteDetalle]
    total: int
    offset: int
    limit: int
