"""Consultas de pago (CU14). Siempre acotadas al propietario de la venta."""

from sqlalchemy import select

from app.modules.cliente_experiencia_compra.shared.models.comercio import (
    MovimientoInv,
    Pago,
    Venta,
)


def locked_venta(db, nroVenta: int):
    query = (select(Venta).where(Venta.nroventa == nroVenta)
             .with_for_update().execution_options(populate_existing=True))
    return db.scalar(query)


def pagos_de_venta(db, nroVenta: int):
    return list(db.scalars(select(Pago).where(Pago.nroventa == nroVenta)
                           .order_by(Pago.idpago)).all())


def locked_pago(db, idPago: int):
    query = (select(Pago).where(Pago.idpago == idPago)
             .with_for_update().execution_options(populate_existing=True))
    return db.scalar(query)


def pago_propio(db, idPago: int, user_id: str):
    return db.scalar(select(Pago).join(Venta, Venta.nroventa == Pago.nroventa)
                     .where(Pago.idpago == idPago, Venta.idusuariocl == user_id))


def venta_de_pago(db, pago: Pago):
    return db.get(Venta, pago.nroventa)


def add_pago(db, row: Pago):
    db.add(row)
    db.flush()
    db.refresh(row, attribute_names=["idpago", "fechahora"])
    return row


def add_movimiento(db, row: MovimientoInv):
    db.add(row)
    db.flush()
    return row
