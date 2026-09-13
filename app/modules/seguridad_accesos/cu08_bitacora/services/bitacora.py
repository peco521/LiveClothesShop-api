from datetime import timezone
from ipaddress import IPv4Address, IPv6Address, ip_address
from typing import get_args

from app.core.errors import DomainError
from app.modules.seguridad_accesos.cu08_bitacora.repositories import bitacora as repository
from app.modules.seguridad_accesos.cu08_bitacora.schemas.bitacora import (
    Accion, BitacoraDetalle, BitacoraListado, BitacoraResumen,
)

KNOWN_ACTIONS = frozenset(get_args(Accion))


def safe_details(action, value):
    if action not in KNOWN_ACTIONS or type(value) is not dict:
        return None
    result = value.get("resultado")
    allowed = ({"rechazado"} if action == "login_rechazado" else
               {"exito", "rechazado"} if action == "recuperacion_solicitada" else {"exito"})
    if type(result) is not str or result not in allowed:
        return None
    # Deliberately omit even rol/agregadas/retiradas: arbitrary identifiers may carry secrets.
    return {"resultado": result}


def safe_ip(value):
    # psycopg can return native inet host objects; SQLite supplies strings.
    if type(value) in (IPv4Address, IPv6Address):
        value = str(value)
    if type(value) is not str or "%" in value or "/" in value:
        return None
    try:
        return str(ip_address(value))
    except ValueError:
        return None


def summary(row, dialect):
    fecha = row.fecha
    if fecha.tzinfo is None or fecha.utcoffset() is None:
        if dialect != "sqlite":
            raise DomainError(500, "error_interno", "No se pudo completar la operación")
        # Explicit SQLite-only convention; never infer the machine's local timezone.
        fecha = fecha.replace(tzinfo=timezone.utc)
    return dict(id=str(row.id), usuario_id=row.usuario_id,
                accion=row.accion if row.accion in KNOWN_ACTIONS else None,
                fecha=fecha.astimezone(timezone.utc))


def list_events(db, filters):
    rows, total = repository.list_events(db, filters)
    dialect = db.get_bind().dialect.name
    return BitacoraListado(items=[BitacoraResumen(**summary(row, dialect)) for row in rows],
                           total=total, offset=filters.offset, limit=filters.limit)


def detail(db, event_id):
    row = repository.get(db, event_id)
    if row is None:
        raise DomainError(404, "bitacora_no_encontrada", "Registro de bitácora no encontrado")
    return BitacoraDetalle(**summary(row, db.get_bind().dialect.name),
                           ip=safe_ip(row.ip), detalles=safe_details(row.accion, row.detalles))
