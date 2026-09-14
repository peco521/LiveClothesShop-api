"""Consultas de venta (CU13). Solo ventas propias en estado consultable."""

from sqlalchemy import select

from app.modules.cliente_experiencia_compra.shared.models.comercio import DetalleVenta, Venta


def registrada_por_carrito(db, idCarrito: int):
    """Venta registrada (pendiente de pago) vinculada al carrito, si existe."""
    return db.scalar(select(Venta).where(Venta.idcarrito == idCarrito,
                                         Venta.estado == "registrada"))


def locked_registrada_por_carrito(db, idCarrito: int):
    query = (select(Venta).where(Venta.idcarrito == idCarrito,
                                 Venta.estado == "registrada")
             .with_for_update().execution_options(populate_existing=True))
    return db.scalar(query)


def propia(db, nroVenta: int, user_id: str):
    return db.scalar(select(Venta).where(Venta.nroventa == nroVenta,
                                         Venta.idusuariocl == user_id))


def detalles(db, nroVenta: int):
    return list(db.scalars(select(DetalleVenta)
                           .where(DetalleVenta.nroventa == nroVenta)
                           .order_by(DetalleVenta.iddetalleventa)).all())


def add_venta(db, row: Venta):
    db.add(row)
    db.flush()
    db.refresh(row, attribute_names=["nroventa", "fechahora"])
    return row


def add_detalle(db, row: DetalleVenta):
    db.add(row)
    db.flush()
    return row
