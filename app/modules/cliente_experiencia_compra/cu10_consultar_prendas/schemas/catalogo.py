from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

TextoBusqueda = Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)]
SortCatalogo = Literal["nombre_asc", "nombre_desc", "precio_asc", "precio_desc"]


class CatalogoFiltros(BaseModel):
    model_config = ConfigDict(extra="forbid")

    offset: int = Field(0, ge=0)
    limit: int = Field(20, ge=1, le=100)
    q: TextoBusqueda = ""
    idCat: int | None = Field(None, ge=1)
    idMarca: int | None = Field(None, ge=1)
    idCol: int | None = Field(None, ge=1)
    idTemp: int | None = Field(None, ge=1)
    idTalla: int | None = None
    idColor: int | None = None
    minPrecio: Decimal | None = Field(None, ge=0)
    maxPrecio: Decimal | None = Field(None, ge=0)
    soloDisponibles: bool = False
    sort: SortCatalogo = "nombre_asc"


class CategoriaResumen(BaseModel):
    idCat: int
    descripcion: str


class MarcaResumen(BaseModel):
    idMarca: int
    nombre: str


class ColeccionResumen(BaseModel):
    idCol: int
    descripcion: str


class PromocionResumen(BaseModel):
    idPromo: int
    nombre: str
    tipoDescuento: str
    valorDescuento: Decimal


class ProductoResumen(BaseModel):
    idProd: str
    descripcion: str
    categoria: CategoriaResumen
    marca: MarcaResumen
    coleccion: ColeccionResumen
    promocion: PromocionResumen | None = None
    precioMin: Decimal | None = None
    precioMax: Decimal | None = None
    imagen: str | None = None
    disponible: bool
    totalVariantes: int


class ProductosListado(BaseModel):
    items: list[ProductoResumen]
    total: int
    offset: int
    limit: int


class TallaDetalle(BaseModel):
    idTalla: int
    descripcion: str


class ColorDetalle(BaseModel):
    idColor: int
    descripcion: str
    hex: str


class VarianteDetalle(BaseModel):
    idVariante: str
    sku: str
    precio: Decimal
    imagen: str | None = None
    talla: TallaDetalle
    colores: list[ColorDetalle] = []


class DisponibilidadSucursal(BaseModel):
    nroSuc: int
    sucursal: str
    ciudad: str
    idVariante: str
    stock: int
    cantDisp: int


class ProductoDetalle(BaseModel):
    idProd: str
    descripcion: str
    estado: str
    categoria: CategoriaResumen
    marca: MarcaResumen
    coleccion: ColeccionResumen
    promocion: PromocionResumen | None = None
    variantes: list[VarianteDetalle] = []
    disponibilidad: list[DisponibilidadSucursal] = []


class FacetaItem(BaseModel):
    id: int
    nombre: str


class FacetasListado(BaseModel):
    items: list[FacetaItem]
    total: int
