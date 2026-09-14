from datetime import date, time
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

IdVariante = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=15)]
EstadoReserva = Literal["pendiente", "confirmada", "atendida", "cancelada"]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReservaItemCrear(Input):
    idVar: IdVariante
    cantidad: int = Field(ge=1)


class ReservaCrear(Input):
    nroSuc: int = Field(ge=1)
    fechaReserva: date
    horaAtencion: time
    items: list[ReservaItemCrear] = Field(min_length=1)


class ReservaItemDetalle(BaseModel):
    idDetalleRes: int
    idVar: str
    sku: str
    producto: str
    cantidad: int


class ReservaSucursal(BaseModel):
    nro: int
    nombre: str
    ciudad: str


class ReservaDetalle(BaseModel):
    nroReserva: int
    fechaReserva: date
    horaAtencion: time
    estado: EstadoReserva
    sucursal: ReservaSucursal
    items: list[ReservaItemDetalle]
    totalUnidades: int
    vencida: bool = False


class ReservasListado(BaseModel):
    items: list[ReservaDetalle]
    total: int
    offset: int
    limit: int


class ReservasFiltros(Input):
    offset: int = Field(0, ge=0)
    limit: int = Field(20, ge=1, le=100)
    estado: EstadoReserva | None = None


class SucursalCliente(BaseModel):
    nro: int
    nombre: str
    direccion: str
    ciudad: str


class SucursalesClienteListado(BaseModel):
    items: list[SucursalCliente]
    total: int


class HorarioRango(BaseModel):
    horaIni: time
    horaFin: time


class HorariosSucursal(BaseModel):
    nroSuc: int
    rangos: list[HorarioRango]
