"""Consultas de carrito (CU12). Solo el carrito ACTIVO del propietario."""

from sqlalchemy import func, select, text

from app.modules.cliente_experiencia_compra.shared.models.comercio import Carrito, DetalleCarro
from app.modules.seguridad_accesos.models import Cliente
from app.modules.cliente_experiencia_compra.shared.repositories.tenant import scoped


def _scoped(db, query):
    return scoped(db, query, Carrito, DetalleCarro, Cliente)


def locked_cliente(db, user_id: str):
    """Fila estable del cliente para serializar la creación del carrito activo."""
    query = (select(Cliente).where(Cliente.idusuario == user_id)
             .with_for_update().execution_options(populate_existing=True))
    return db.scalar(_scoped(db, query))


def active_cart(db, user_id: str):
    return db.scalar(_scoped(db, select(Carrito).where(Carrito.idusuariocl == user_id,
                                           Carrito.estado == "activo")))


def locked_active_cart(db, user_id: str):
    query = (select(Carrito).where(Carrito.idusuariocl == user_id,
                                   Carrito.estado == "activo")
             .with_for_update().execution_options(populate_existing=True))
    return db.scalar(_scoped(db, query))


def cart_details(db, idCarrito: int):
    return list(db.scalars(_scoped(db, select(DetalleCarro)
                           .where(DetalleCarro.idcarrito == idCarrito)
                           .order_by(DetalleCarro.iddetallecarro))).all())


def locked_detail(db, idCarrito: int, idDetalle: int):
    query = (select(DetalleCarro)
             .where(DetalleCarro.idcarrito == idCarrito,
                    DetalleCarro.iddetallecarro == idDetalle)
             .with_for_update().execution_options(populate_existing=True))
    return db.scalar(_scoped(db, query))


def detail_by_variant(db, idCarrito: int, idVar: str):
    query = (select(DetalleCarro)
             .where(DetalleCarro.idcarrito == idCarrito,
                    DetalleCarro.idvar == idVar)
             .with_for_update().execution_options(populate_existing=True))
    return db.scalar(_scoped(db, query))


def next_detail_id(db, idCarrito: int) -> int:
    current = db.scalar(_scoped(db, select(func.max(DetalleCarro.iddetallecarro))
                         .where(DetalleCarro.idcarrito == idCarrito))) or 0
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


def agregar_con_procedimiento(db, user_id: str, id_var: str, cantidad: int):
    if db.info.get("multitenant") is True:
        db.execute(text("CALL sp_agregar_al_carrito(CAST(:tenant AS integer), "
                        "CAST(:user AS varchar), CAST(:variant AS varchar), CAST(:quantity AS integer))"),
                   {"tenant": db.info.get("idemp") or 0, "user": user_id,
                    "variant": id_var, "quantity": cantidad})
        return
    db.execute(text("CALL sp_agregar_al_carrito(:user_id, :id_var, :cantidad)"), {
        "user_id": user_id, "id_var": id_var, "cantidad": cantidad,
    })
