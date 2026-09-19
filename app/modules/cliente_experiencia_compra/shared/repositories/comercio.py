"""Reservas e inventario compartidos (CU11; reutilizable por CU13/CU14/CU15/CU24)."""

from sqlalchemy import func, select

from app.modules.cliente_experiencia_compra.shared.models.catalogo import Inventario
from app.modules.cliente_experiencia_compra.shared.models.comercio import (
    DetalleReserva,
    HorarioAtencion,
    HorarioSuc,
    Reserva,
)
from app.modules.seguridad_accesos.shared.models import Sucursal


def locked_inventario(db, idVar: str, nroSuc: int):
    """Fila de inventario bloqueada (SELECT ... FOR UPDATE en PostgreSQL)."""
    query = (select(Inventario)
             .where(Inventario.idvar == idVar, Inventario.nrosuc == nroSuc)
             .with_for_update()
             .execution_options(populate_existing=True))
    return db.scalar(query)


def horarios_sucursal(db, nroSuc: int):
    """Rangos [horaIni, horaFin] declarados para la sucursal (solo lectura)."""
    return list(db.execute(select(HorarioAtencion.horaini, HorarioAtencion.horafin, HorarioSuc.dias)
                           .join(HorarioSuc, HorarioSuc.idaten == HorarioAtencion.idaten)
                           .where(HorarioSuc.nrosuc == nroSuc)
                           .order_by(HorarioAtencion.horaini)).all())


def disponibilidad_global(db, variant_ids):
    """Suma de cantDisp por variante en sucursales activas (el carrito no tiene sucursal)."""
    if not variant_ids:
        return {}
    rows = db.execute(select(Inventario.idvar, func.coalesce(func.sum(Inventario.cantdisp), 0))
                      .join(Sucursal, (Sucursal.nro == Inventario.nrosuc) & (Sucursal.estado == "activo"))
                      .where(Inventario.idvar.in_(variant_ids))
                      .group_by(Inventario.idvar)).all()
    return {var_id: int(total or 0) for var_id, total in rows}


def locked_inventarios(db, idVar: str, nroSuc: int):
    """Todas las filas de inventario de la variante en la sucursal, bloqueadas y ordenadas."""
    query = (select(Inventario)
             .where(Inventario.idvar == idVar, Inventario.nrosuc == nroSuc)
             .order_by(Inventario.nroinv)
             .with_for_update()
             .execution_options(populate_existing=True))
    return list(db.scalars(query).all())


def entregar_reserva(db, venta):
    """Entrega la reserva vinculada a una venta de caja al confirmarse su cobro (CU22 + CU24).

    Devuelve al disponible lo que la reserva retenía y actualiza el estado a
    ``atendida``: la reserva sólo se atiende cuando el cliente pagó. Es
    idempotente (si ya no está ``confirmada`` no hace nada) y debe ejecutarse
    ANTES del descuento de la venta, para que el disponible liberado cubra la
    salida y no queden unidades retenidas por una reserva ya entregada.
    """
    if venta.nroreserva is None:
        return
    reserva = db.scalar(select(Reserva)
                        .where(Reserva.nroreserva == venta.nroreserva)
                        .with_for_update()
                        .execution_options(populate_existing=True))
    if reserva is None or reserva.estado != "confirmada":
        return
    detalles = db.scalars(select(DetalleReserva)
                          .where(DetalleReserva.nroreserva == reserva.nroreserva)
                          .order_by(DetalleReserva.iddetalleres)).all()
    for detalle in detalles:
        left = detalle.cantidad
        for inventory in locked_inventarios(db, detalle.idvar, reserva.nrosuc):
            amount = min(left, inventory.stock - inventory.cantdisp)
            if amount <= 0:
                continue
            inventory.cantdisp += amount
            left -= amount
            if left == 0:
                break
    reserva.estado = "atendida"
    db.flush()
