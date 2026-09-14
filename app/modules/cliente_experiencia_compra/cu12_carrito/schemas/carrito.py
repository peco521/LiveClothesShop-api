from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

IdVariante = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=15)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ItemAgregar(Input):
    idVar: IdVariante
    cantidad: int = Field(ge=1)


class ItemCantidad(Input):
    cantidad: int = Field(ge=1)


class ItemTalla(BaseModel):
    idTalla: int
    descripcion: str


class ItemColor(BaseModel):
    idColor: int
    descripcion: str
    hex: str


class ItemPromocion(BaseModel):
    idPromo: int
    nombre: str
    tipoDescuento: str
    valorDescuento: Decimal


class CarritoItem(BaseModel):
    idDetalleCarro: int
    idVar: str
    sku: str
    imagen: str | None = None
    producto: str
    talla: ItemTalla
    colores: list[ItemColor] = []
    precio: Decimal
    promocion: ItemPromocion | None = None
    cantidad: int
    subtotal: Decimal
    disponible: bool
    cantidadDisponible: int


class CarritoDetalle(BaseModel):
    idCarrito: int | None
    items: list[CarritoItem] = []
    cantidadItems: int = 0
    subtotal: Decimal = Decimal("0")
