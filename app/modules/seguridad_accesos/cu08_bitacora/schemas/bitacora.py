import re
from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_serializer, model_validator


Accion = Literal[
    "cliente_registrado", "login_correcto", "login_rechazado", "logout_correcto",
    "recuperacion_solicitada", "contrasena_restablecida", "usuario_creado", "empleado_creado",
    "usuario_actualizado", "empleado_actualizado", "usuario_activado", "usuario_desactivado",
    "rol_creado", "rol_actualizado", "permisos_rol_actualizados",
    "cliente_actualizado", "cliente_activado", "cliente_desactivado",
    "ciudad_creada", "ciudad_actualizada", "sucursal_creada", "sucursal_actualizada", "sucursal_estado_actualizado",
    "reserva_creada", "reserva_cancelada", "reserva_vencida",
    "carrito_item_agregado", "carrito_item_actualizado", "carrito_item_eliminado",
    "venta_registrada", "pago_iniciado", "pago_aprobado", "pago_rechazado", "venta_anulada",
    "catalogo_guardado", "catalogo_eliminado", "proveedor_guardado", "proveedor_eliminado", "inventario_movimiento_registrado",
]


def iso_aware(value):
    # No numeric timestamps, implicit local timezone or sub-microsecond truncation.
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
        r"(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])", value
    ):
        raise ValueError("Fecha ISO con zona requerida")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise ValueError("Fecha fuera del rango admitido") from None


class BitacoraFiltros(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accion: Accion | None = None
    usuario_id: str | None = Field(None, min_length=1, max_length=100)
    desde: Annotated[datetime, BeforeValidator(iso_aware)] | None = None
    hasta: Annotated[datetime, BeforeValidator(iso_aware)] | None = None
    offset: int = Field(0, ge=0, le=9223372036854775807)
    limit: int = Field(20, ge=1, le=100)

    @model_validator(mode="after")
    def ordered_range(self):
        if self.desde is not None and self.hasta is not None and self.desde > self.hasta:
            raise ValueError("Rango de fechas inválido")
        return self


class BitacoraResultado(BaseModel):
    resultado: Literal["exito", "rechazado"]


class BitacoraResumen(BaseModel):
    id: str
    usuario_id: str | None
    accion: Accion | None
    fecha: datetime
    ip: str | None

    @field_serializer("fecha")
    def serialize_fecha(self, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Fecha sin zona")
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class BitacoraDetalle(BitacoraResumen):
    detalles: BitacoraResultado | None


class BitacoraListado(BaseModel):
    items: list[BitacoraResumen]
    total: int
    offset: int
    limit: int
