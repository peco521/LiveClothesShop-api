from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints, model_validator, field_validator

Estado = Literal['activo', 'inactivo']


def texto(length):
    return Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=length)]


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')


class TallaDatos(Input):
    descripcion: texto(8)


class ColorDatos(Input):
    descripcion: texto(20)
    hex: Annotated[str, StringConstraints(pattern=r'^#[0-9A-Fa-f]{6}$')]


class CategoriaDatos(Input):
    descripcion: texto(30)


class MarcaDatos(Input):
    nombre: texto(30)
    estado: Estado = 'activo'


class ColeccionDatos(Input):
    descripcion: texto(150)
    idTemps: list[Annotated[int, Field(ge=1)]] = Field(default_factory=list, max_length=100)

    @field_validator('idTemps')
    @classmethod
    def unique(cls, value):
        if len(value) != len(set(value)):
            raise ValueError('Temporadas repetidas')
        return value


class TemporadaDatos(Input):
    nombre: texto(100)
    fechaIni: date
    fechaFin: date
    estado: Estado = 'activo'

    @model_validator(mode='after')
    def dates(self):
        if self.fechaFin < self.fechaIni:
            raise ValueError('La fecha final no puede ser anterior a la inicial')
        return self


class ProveedorDatos(Input):
    nombre: texto(100)
    correo: EmailStr = Field(max_length=150)
    direccion: texto(150)
    telefono: texto(20)


class VarianteDatos(Input):
    idVariante: texto(15) | None = None
    sku: texto(30)
    precio: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    estado: Estado = 'activo'
    img: str | None = Field(default=None, max_length=255)
    idTalla: int = Field(ge=-32768, le=32767)
    idColores: list[Annotated[int, Field(ge=-32768, le=32767)]] = Field(min_length=1, max_length=100)

    @field_validator('img')
    @classmethod
    def image_url(cls, value):
        if value is None or not value.strip():
            return None
        from urllib.parse import urlsplit
        parsed = urlsplit(value.strip())
        if parsed.scheme not in {'https', 'http'} or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError('La imagen debe ser una URL HTTP o HTTPS')
        return value.strip()

    @field_validator('idColores')
    @classmethod
    def unique_colors(cls, value):
        if len(value) != len(set(value)):
            raise ValueError('Colores repetidos')
        return value


class ProductoDatos(Input):
    descripcion: texto(100)
    estado: Estado = 'activo'
    idCat: int = Field(ge=1)
    idMarca: int = Field(ge=1)
    idCol: int = Field(ge=1)
    idProv: int = Field(ge=1)
    idPromo: int | None = Field(default=None, ge=1)
    variantes: list[VarianteDatos] = Field(min_length=1, max_length=100)

    @model_validator(mode='after')
    def unique_variants(self):
        skus = [v.sku for v in self.variantes]
        ids = [v.idVariante for v in self.variantes if v.idVariante is not None]
        if len(skus) != len(set(skus)):
            raise ValueError('SKU duplicado')
        if len(ids) != len(set(ids)):
            raise ValueError('Variante repetida')
        return self
