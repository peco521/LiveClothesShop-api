from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, EmailStr, Field, StrictBool, StringConstraints, field_validator, model_validator

from app.modules.seguridad_accesos.schemas.auth import PublicInput, Registro, RolResponse, normalize_email

Code = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=10)]
RoleId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=15)]
Text50 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]
Text100 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
BranchId = Annotated[int, Field(strict=True, ge=-2147483648, le=2147483647)]


class EmpleadoCrear(Registro):
    nroRol: RoleId
    cod_emp: Code
    cargo: Text50
    nroSuc: BranchId


class EmpleadoEditar(PublicInput):
    ci: Text100 | None = None
    nombres: Text100 | None = None
    apellidoPat: Text50 | None = None
    apellidoMat: Text50 | None = None
    sexo: Literal["M", "F"] | None = None
    correo: EmailStr | None = Field(default=None, max_length=100)
    telefono: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20)] | None = None
    direccion: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=150)] | None = None
    fechaNac: date | None = None
    nroRol: RoleId | None = None
    cod_emp: Code | None = None
    cargo: Text50 | None = None
    nroSuc: BranchId | None = None

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


class UsuarioEstado(PublicInput):
    activo: StrictBool


class EmpleadoPerfil(BaseModel):
    cod_emp: str
    cargo: str
    nroSuc: int


class AdminPerfil(BaseModel):
    cod_adm: str


class UsuarioDetalle(BaseModel):
    idUsuario: str
    ci: str
    nombres: str | None
    apellidoPat: str
    apellidoMat: str
    sexo: Literal["M", "F"]
    correo: str
    telefono: str
    direccion: str
    fechaNac: date
    tipo: Literal["A", "E"]
    activo: bool
    nroRol: str
    rol: RolResponse
    empleado: EmpleadoPerfil | None
    admin: AdminPerfil | None


class UsuariosListado(BaseModel):
    items: list[UsuarioDetalle]
    total: int
    offset: int
    limit: int


class CiudadOpcion(BaseModel):
    id: int
    nombre: str


class SucursalOpcion(BaseModel):
    nro: int
    nombre: str
    direccion: str
    estado: Literal["activo", "inactivo"]
    idCiud: int
    ciudad: CiudadOpcion
