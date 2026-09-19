from typing import Annotated, Literal
import unicodedata

from pydantic import AfterValidator, BaseModel, StringConstraints, field_validator

from app.modules.seguridad_accesos.schemas.auth import PublicInput


def identifier(value: str):
    # Preserve identity/case exactly. Reject ambiguous path segments rather than
    # silently normalizing a PK. No ASCII-only/business-code restriction.
    if value != value.strip() or value in {".", ".."} or any(
        c in "/\\%?#" or unicodedata.category(c).startswith("C") for c in value
    ):
        raise ValueError("Identificador no compatible con la ruta")
    return value


Identifier = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=15), AfterValidator(identifier)]
Description = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]


class RolCrear(PublicInput):
    nro: Identifier
    descripcion: Description


class RolEditar(PublicInput):
    descripcion: Description


class PermisosReemplazar(PublicInput):
    permisos: list[Identifier]

    @field_validator("permisos")
    @classmethod
    def unique(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("Funciones duplicadas")
        return value


class RolDetalle(BaseModel):
    nro: str
    descripcion: str
    esRolCliente: bool
    # CU06: baja lógica del rol (la columna ya existe en la base de datos).
    estado: Literal["activo", "inactivo"] = "activo"


class EstadoCuenta(PublicInput):
    activo: bool


class RolesListado(BaseModel):
    items: list[RolDetalle]
    total: int
    offset: int
    limit: int


class FuncionDetalle(BaseModel):
    id: str
    descripcion: str | None


class PermisosDetalle(BaseModel):
    nroRol: str
    esRolCliente: bool
    permisos: list[str]
