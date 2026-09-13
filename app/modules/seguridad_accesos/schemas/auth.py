from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr, StringConstraints, field_validator


def normalize_email(value):
    return value.strip().lower() if isinstance(value, str) else value


class PublicInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Registro(PublicInput):
    ci: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    nombres: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    apellidoPat: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]
    apellidoMat: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]
    sexo: Literal["M", "F"]
    correo: EmailStr = Field(max_length=100)
    telefono: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20)]
    direccion: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=150)]
    fechaNac: date
    contrasena: SecretStr = Field(min_length=12, max_length=128)

    _correo = field_validator("correo", mode="before")(normalize_email)

    @field_validator("contrasena")
    @classmethod
    def password_not_blank(cls, value):
        if not value.get_secret_value().strip():
            raise ValueError("Contraseña inválida")
        return value

    @field_validator("fechaNac")
    @classmethod
    def birth_date_not_future(cls, value):
        if value > date.today():
            raise ValueError("Fecha inválida")
        return value


class Login(PublicInput):
    correo: EmailStr = Field(max_length=100)
    contrasena: SecretStr = Field(min_length=1, max_length=128)
    _correo = field_validator("correo", mode="before")(normalize_email)


class RecuperarContrasena(PublicInput):
    correo: EmailStr = Field(max_length=100)
    _correo = field_validator("correo", mode="before")(normalize_email)


class RestablecerContrasena(PublicInput):
    token: SecretStr = Field(min_length=43, max_length=43)
    nueva_contrasena: SecretStr = Field(min_length=12, max_length=128)
    _password = field_validator("nueva_contrasena")(Registro.password_not_blank.__func__)


class RegistroResponse(BaseModel):
    idUsuario: str
    correo: str
    mensaje: str = "Cliente registrado correctamente"


class UsuarioResponse(BaseModel):
    idUsuario: str
    nombres: str | None
    correo: str


class RolResponse(BaseModel):
    nro: str
    descripcion: str


class AuthResponse(BaseModel):
    usuario: UsuarioResponse
    rol: RolResponse
    permisos: list[str]
    expiraEn: datetime
