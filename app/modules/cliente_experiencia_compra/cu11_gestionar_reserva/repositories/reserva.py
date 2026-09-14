"""Consultas de reserva (CU11). El inventario se gestiona en shared comercio."""

from sqlalchemy import func, select

from app.modules.cliente_experiencia_compra.shared.models.catalogo import Producto, VarianteProd
from app.modules.cliente_experiencia_compra.shared.models.comercio import DetalleReserva, Reserva


def locked_reserva(db, nroReserva: int):
    query = (select(Reserva).where(Reserva.nroreserva == nroReserva)
             .with_for_update().execution_options(populate_existing=True))
    return db.scalar(query)


def detalles(db, nroReserva: int):
    return list(db.scalars(select(DetalleReserva)
                           .where(DetalleReserva.nroreserva == nroReserva)
                           .order_by(DetalleReserva.iddetalleres)).all())


def variantes_productos(db, idVars):
    """Variantes con su producto para validación y descripción (una sola query)."""
    if not idVars:
        return {}
    rows = db.execute(select(VarianteProd, Producto)
                      .join(Producto, Producto.idprod == VarianteProd.idprod)
                      .where(VarianteProd.idvariante.in_(idVars))).all()
    return {variant.idvariante: (variant, product) for variant, product in rows}


def list_reservas(db, user_id: str, estado: str | None, offset: int, limit: int):
    conditions = [Reserva.idusuariocl == user_id]
    if estado is not None:
        conditions.append(Reserva.estado == estado)
    total = db.scalar(select(func.count()).select_from(Reserva).where(*conditions)) or 0
    rows = list(db.scalars(select(Reserva).where(*conditions)
                           .order_by(Reserva.fechareserva.desc(), Reserva.nroreserva.desc())
                           .offset(offset).limit(limit)).all())
    return rows, total


def vencidas(db, user_id: str, hoy):
    """Pendientes propias con fecha pasada (para liberación lazy)."""
    return list(db.scalars(select(Reserva)
                           .where(Reserva.idusuariocl == user_id,
                                  Reserva.estado == "pendiente",
                                  Reserva.fechareserva < hoy)
                           .order_by(Reserva.nroreserva)).all())


def add_reserva(db, row: Reserva):
    db.add(row)
    db.flush()
    return row


def add_detalle(db, row: DetalleReserva):
    db.add(row)
    db.flush()
    return row
