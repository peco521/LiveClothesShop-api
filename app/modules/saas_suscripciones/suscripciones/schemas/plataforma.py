from datetime import date
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr, StringConstraints, field_validator


def _normalize_email(value):
    return value.strip().lower() if isinstance(value, str) else value


class PlatformInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmpresaResumen(BaseModel):
    idEmp: int
    codigo: str
    nombre: str
    estado: str
    logoUrl: str | None = None
    colorPrimario: str | None = None
    colorSecundario: str | None = None
    esDemostracion: bool = False
    plan: str | None = None
    idPlan: int | None = None
    maxSucursales: int | None = None
    suscripcionEstado: str | None = None
    fechaInicio: date | None = None
    fechaVencimiento: date | None = None
    sucursalesActivas: int = 0


class IdentidadVisual(PlatformInput):
    nombre: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)] | None = None
    logoUrl: Annotated[str, StringConstraints(strip_whitespace=True, max_length=255)] | None = None
    colorPrimario: Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^#[0-9A-Fa-f]{6}$")] | None = None
    colorSecundario: Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^#[0-9A-Fa-f]{6}$")] | None = None

    @field_validator("logoUrl")
    @classmethod
    def logo_seguro(cls, value):
        if value is None or value == "":
            return value
        if value.startswith("/brands/") and ".." not in value.split("/") and "\\" not in value:
            return value
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("El logo debe ser una ruta /brands/ local o una URL HTTPS válida")
        return value


class NuevoAdministrador(PlatformInput):
    idUsuario: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)] | None = None
    ci: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    nombre: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    apellidoPat: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]
    apellidoMat: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]
    sexo: Literal["M", "F"]
    correo: EmailStr = Field(max_length=100)
    telefono: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20)]
    direccion: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=150)]
    fechaNac: date
    # Contraseña inicial en claro solo en el transporte TLS interno; se guarda
    # como hash Argon2id y nunca se devuelve en la respuesta.
    contrasena: SecretStr = Field(min_length=12, max_length=128)

    _correo = field_validator("correo", mode="before")(_normalize_email)

    @field_validator("idUsuario", mode="before")
    @classmethod
    def _normalizar_id_usuario(cls, value):
        if isinstance(value, str) and not value.strip():
            return None
        return value


class NuevaEmpresa(PlatformInput):
    codigo: Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[A-Za-z0-9-]{1,30}$")] | None = None
    nombre: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    nit: Annotated[str, StringConstraints(strip_whitespace=True, max_length=30)] | None = None
    idPlan: int = Field(ge=1, le=32767)
    fechaInicio: date
    fechaVencimiento: date
    administrador: NuevoAdministrador

    @field_validator("codigo", mode="before")
    @classmethod
    def _normalizar_codigo(cls, value):
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("fechaVencimiento")
    @classmethod
    def rango_valido(cls, value, info):
        inicio = info.data.get("fechaInicio")
        if inicio is not None and value <= inicio:
            raise ValueError("La fecha de vencimiento debe ser posterior al inicio")
        return value


class CambioEstado(PlatformInput):
    estado: Literal["activa", "suspendida", "inactiva"]


class CambioPlan(PlatformInput):
    idPlan: int = Field(ge=1, le=32767)
    fechaInicio: date
    fechaVencimiento: date


class PlanResumen(BaseModel):
    idPlan: int
    nombre: str
    maxSucursales: int
    estado: str
    funcionalidades: list[str] = Field(default_factory=list)


class UsoSucursales(BaseModel):
    usadas: int
    limite: int


class AccesoApoyo(PlatformInput):
    """Acceso de soporte: explícito, acotado y auditado.

    NO emite una sesión de empresa: entrega una lectura resumida de la empresa
    autorizada. Así ninguna sesión cambia de tenant enviando un idEmp.
    """

    idEmp: int = Field(gt=0)
    motivo: Annotated[str, StringConstraints(strip_whitespace=True, min_length=10, max_length=300)]


class AuditoriaEntrada(BaseModel):
    id: int
    accion: str
    usuarioId: str | None = None
    fecha: str
    detalles: dict | None = None
