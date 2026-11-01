from datetime import date, datetime, time
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

IdVariante = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20)]


class SyncInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LineaVenta(SyncInput):
    idVariante: IdVariante
    cantidad: int = Field(ge=1, le=100)
    precio: Decimal = Field(gt=0, le=Decimal("99999999.99"))


class DatosVenta(SyncInput):
    lineas: list[LineaVenta] = Field(min_length=1, max_length=50)
    metodo: Literal["efectivo", "tarjeta", "QR", "transferencia"] = "efectivo"
    referencia: Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)] | None = None
    # Descuento calculado por el cliente: el servidor lo recalcula con el
    # catálogo vigente y rechaza cualquier diferencia.
    descuento: Decimal | None = Field(default=None, ge=0, le=Decimal("99999999.99"))
    idCliente: Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)] | None = None

    @field_validator("lineas")
    @classmethod
    def variantes_distintas(cls, value):
        ids = [linea.idVariante for linea in value]
        if len(ids) != len(set(ids)):
            raise ValueError("No se puede repetir una variante en la misma venta")
        return value


class LineaReserva(SyncInput):
    idVariante: IdVariante
    cantidad: int = Field(ge=1, le=100)


class DatosReserva(SyncInput):
    lineas: list[LineaReserva] = Field(min_length=1, max_length=20)
    fecha: date
    hora: time

    @field_validator("lineas")
    @classmethod
    def variantes_distintas(cls, value):
        ids = [linea.idVariante for linea in value]
        if len(ids) != len(set(ids)):
            raise ValueError("No se puede repetir una variante en la misma reserva")
        return value


class Operacion(SyncInput):
    id: UUID
    tipo: Literal["venta", "reserva"]
    # Versión del formato del payload. Un cambio incompatible sube este número
    # y el servidor rechaza versiones que no conoce.
    version: Literal[1] = 1
    fecha: datetime
    sucursal: int = Field(gt=0)
    datos: dict
    dependeDe: list[UUID] = Field(default_factory=list, max_length=20)


class LoteSync(SyncInput):
    dispositivo: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)] | None = None
    # Origen real del dispositivo; se valida contra el CHECK de venta.origen.
    origen: Literal["pwa", "movil"] = "pwa"
    operaciones: list[Operacion] = Field(min_length=1, max_length=200)


class ResultadoOperacion(BaseModel):
    id: UUID
    # Estado del servidor. El cliente lo mapea a su propia máquina de estados.
    estado: Literal["sincronizada", "en_proceso", "rechazada", "conflicto", "pendiente"]
    codigo: str | None = None
    detalle: str | None = None
    nroVenta: int | None = None
    nroReserva: int | None = None
    intentos: int = 0
    # True cuando el resultado proviene de un registro previo (reintento).
    idempotente: bool = False


class ResultadoLote(BaseModel):
    empresa: str
    dispositivo: str | None = None
    resultados: list[ResultadoOperacion]
    sincronizadas: int
    pendientes: int
    conflictos: int
    rechazadas: int


class OperacionHistorial(BaseModel):
    id: UUID
    tipo: str
    estado: str
    intentos: int
    fechaLocal: datetime
    fechaRecepcion: datetime
    fechaResolucion: datetime | None = None
    codigoMotivo: str | None = None
    detalleMotivo: str | None = None
    nroVenta: int | None = None
    nroReserva: int | None = None


class HistorialSync(BaseModel):
    total: int
    operaciones: list[OperacionHistorial]


class ResolucionConflicto(SyncInput):
    detalle: Annotated[str, StringConstraints(strip_whitespace=True, min_length=5, max_length=300)]
