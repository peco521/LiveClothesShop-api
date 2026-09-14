"""Consultas de carrito (CU12). Solo el carrito ACTIVO del propietario."""

from sqlalchemy import func, select

from app.modules.cliente_experiencia_compra.shared.models.comercio import Carrito, DetalleCarro
from app.modules.seguridad_accesos.models import Cliente


def locked_cliente(db, user_id: str):
    """Fila estable del cliente para serializar la creación del carrito activo."""
    query = (select(Cliente).where(Cliente.idusuario == user_id)
             .with_for_update().execution_options(populate_existing=True))
    return db.scalar(query)


def active_cart(db, user_id: str):
    return db.scalar(select(Carrito).where(Carrito.idusuariocl == user_id,
                                           Carrito.estado == "activo"))


def locked_active_cart(db, user_id: str):
    query = (select(Carrito).where(Carrito.idusuariocl == user_id,
                                   Carrito.estado == "activo")
             .with_for_update().execution_options(populate_existing=True))
    return db.scalar(query)


def cart_details(db, idCarrito: int):
    return list(db.scalars(select(DetalleCarro)
                           .where(DetalleCarro.idcarrito == idCarrito)
                           .order_by(DetalleCarro.iddetallecarro)).all())


def locked_detail(db, idCarrito: int, idDetalle: int):
    query = (select(DetalleCarro)
             .where(DetalleCarro.idcarrito == idCarrito,
                    DetalleCarro.iddetallecarro == idDetalle)
             .with_for_update().execution_options(populate_existing=True))
    return db.scalar(query)


def detail_by_variant(db, idCarrito: int, idVar: str):
    query = (select(DetalleCarro)
             .where(DetalleCarro.idcarrito == idCarrito,
                    DetalleCarro.idvar == idVar)
             .with_for_update().execution_options(populate_existing=True))
    return db.scalar(query)


def next_detail_id(db, idCarrito: int) -> int:
    current = db.scalar(select(func.max(DetalleCarro.iddetallecarro))
                        .where(DetalleCarro.idcarrito == idCarrito)) or 0
    return int(current) + 1


def add_cart(db, row: Carrito):
    db.add(row)
    db.flush()
    return row


def add_detail(db, row: DetalleCarro):
    db.add(row)
    db.flush()
    return row


def remove_detail(db, row: DetalleCarro):
    db.delete(row)
    db.flush()
