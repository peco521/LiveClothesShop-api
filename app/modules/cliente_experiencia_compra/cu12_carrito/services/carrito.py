"""CU12 Carrito: intención de compra sin reservar stock.

El carrito valida contra el cantDisp ACTUAL pero nunca lo modifica.
Precios y promociones usan las mismas reglas del catálogo (shared precios).
"""

from contextlib import contextmanager
from decimal import Decimal

from sqlalchemy.exc import DBAPIError, IntegrityError

from app.core.database import is_postgresql, sqlstate
from app.core.errors import DomainError
from app.modules.cliente_experiencia_compra.cu11_gestionar_reserva.repositories import reserva as reserva_repo
from app.modules.cliente_experiencia_compra.cu12_carrito.repositories import carrito as repository
from app.modules.cliente_experiencia_compra.cu12_carrito.schemas.carrito import (
    CarritoDetalle,
    CarritoItem,
    ItemAgregar,
    ItemCantidad,
    ItemColor,
    ItemPromocion,
    ItemTalla,
)
from app.modules.cliente_experiencia_compra.shared.models.comercio import Carrito, DetalleCarro
from app.modules.cliente_experiencia_compra.shared.repositories import catalogo as catalogo_repo
from app.modules.cliente_experiencia_compra.shared.repositories import comercio as comercio_repo
from app.modules.cliente_experiencia_compra.shared.services.precios import promocion_vigente
from app.modules.seguridad_accesos.services.bitacora import record
from app.modules.cliente_experiencia_compra.cu13_compra_digital.repositories import venta as venta_repo


def _permitir_edicion(db, user_id):
    # Same outer lock in checkout, payment and cart edits prevents lock inversion.
    if repository.locked_cliente(db, user_id) is None:
        raise DomainError(403, "acceso_denegado", "No tiene autorización para esta operación")
    cart = repository.locked_active_cart(db, user_id)
    if cart and venta_repo.registrada_por_carrito(db, cart.idcarrito):
        raise DomainError(409, "compra_pendiente", "Cancela la compra pendiente antes de editar el carrito")


@contextmanager
def transaction(db):
    # require_cliente ya abrió esta transacción en la sesión compartida.
    try:
        yield
        db.commit()
    except IntegrityError:
        db.rollback()
        raise DomainError(409, "conflicto_integridad", "Los datos entran en conflicto con un registro existente") from None
    except DBAPIError as exc:
        db.rollback()
        if sqlstate(exc) == "P0001":
            raise DomainError(409, "disponibilidad_insuficiente",
                              "No hay disponibilidad suficiente para esta prenda") from None
        raise
    except Exception:
        db.rollback()
        raise


def _variante_activa(db, idVar: str):
    found = reserva_repo.variantes_productos(db, [idVar.strip()])
    pair = found.get(idVar.strip())
    if pair is None:
        raise DomainError(404, "variante_no_encontrada", "Variante no encontrada")
    variant, product = pair
    if variant.estado != "activo":
        raise DomainError(404, "variante_no_encontrada", "Variante no encontrada")
    if product.estado != "activo":
        raise DomainError(404, "producto_no_encontrado", "Prenda no encontrada")
    return variant, product


def _construir(db, cart: Carrito | None):
    if cart is None:
        return CarritoDetalle(idCarrito=None, items=[], cantidadItems=0, subtotal=Decimal("0"))
    details = repository.cart_details(db, cart.idcarrito)
    if not details:
        return CarritoDetalle(idCarrito=cart.idcarrito, items=[], cantidadItems=0, subtotal=Decimal("0"))
    var_ids = sorted({d.idvar for d in details})
    found = reserva_repo.variantes_productos(db, var_ids)
    colors = catalogo_repo.variant_colors(db, var_ids)
    tallas = catalogo_repo.tallas_map(db, list({v.idtalla for v, _ in found.values()}))
    promos = catalogo_repo.promociones(db, [p.idpromo for _, p in found.values()])
    disp = comercio_repo.disponibilidad_global(db, var_ids)
    items = []
    for detail in details:
        pair = found.get(detail.idvar)
        if pair is None:
            continue  # Variante eliminada del catálogo: no se borra, se omite del resumen.
        variant, product = pair
        promo = promos.get(product.idpromo)
        vigente = promocion_vigente(promo)
        talla = tallas.get(variant.idtalla)
        disponible = (variant.estado == "activo" and product.estado == "activo"
                      and detail.cantidad <= disp.get(detail.idvar, 0))
        subtotal = variant.precio * detail.cantidad
        items.append(CarritoItem(
            idDetalleCarro=detail.iddetallecarro, idVar=detail.idvar,
            sku=variant.sku, imagen=variant.img, producto=product.descripcion,
            talla=ItemTalla(idTalla=talla.idtalla, descripcion=talla.descripcion)
            if talla else ItemTalla(idTalla=variant.idtalla, descripcion=str(variant.idtalla)),
            colores=[ItemColor(idColor=c.idcolor, descripcion=c.descripcion, hex=c.hex)
                     for c in colors.get(detail.idvar, [])],
            precio=variant.precio,
            promocion=ItemPromocion(idPromo=promo.idpromo, nombre=promo.nombre,
                                    tipoDescuento=promo.tipodescuento,
                                    valorDescuento=promo.valordescuento) if vigente else None,
            cantidad=detail.cantidad, subtotal=subtotal, disponible=disponible,
            cantidadDisponible=disp.get(detail.idvar, 0)))
    total_unidades = sum(i.cantidad for i in items)
    total = sum((i.subtotal for i in items), Decimal("0"))
    return CarritoDetalle(idCarrito=cart.idcarrito, items=items,
                          cantidadItems=total_unidades, subtotal=total)


def obtener(db, user_id: str):
    # GET read-only: jamás crea carrito.
    cart = repository.active_cart(db, user_id)
    return _construir(db, cart)


def _activo_o_crear(db, user_id: str):
    # Serializa la creación: bloquea la fila Cliente, revalida y recién crea.
    if repository.locked_cliente(db, user_id) is None:
        raise DomainError(403, "acceso_denegado", "No tiene autorización para esta operación")
    cart = repository.active_cart(db, user_id)
    if cart is None:
        cart = repository.add_cart(db, Carrito(idusuariocl=user_id, estado="activo"))
    return repository.locked_active_cart(db, user_id) or cart


def agregar(db, data: ItemAgregar, user_id: str, peer):
    with transaction(db):
        _permitir_edicion(db, user_id)
        variant, _ = _variante_activa(db, data.idVar)
        if is_postgresql(db):
            cart_previo = repository.active_cart(db, user_id)
            ya_existia = (cart_previo is not None and
                          repository.detail_by_variant(
                              db, cart_previo.idcarrito, variant.idvariante) is not None)
            repository.agregar_con_procedimiento(
                db, user_id, variant.idvariante, data.cantidad)
            # CALL modifica filas fuera del seguimiento del ORM.
            db.expire_all()
            cart = repository.active_cart(db, user_id)
            if cart is None:  # Defensa ante una instalación incompleta de la BD.
                raise DomainError(503, "carrito_no_disponible",
                                  "No se pudo actualizar el carrito")
            record(db, "carrito_item_actualizado" if ya_existia else
                   "carrito_item_agregado", user_id, peer, True)
            return _construir(db, cart)

        cart = _activo_o_crear(db, user_id)
        detail = repository.detail_by_variant(db, cart.idcarrito, variant.idvariante)
        if detail is None:
            if data.cantidad > comercio_repo.disponibilidad_global(db, [variant.idvariante]).get(variant.idvariante, 0):
                raise DomainError(409, "disponibilidad_insuficiente",
                                  "No hay disponibilidad suficiente para esta prenda")
            repository.add_detail(db, DetalleCarro(
                idcarrito=cart.idcarrito,
                iddetallecarro=repository.next_detail_id(db, cart.idcarrito),
                idvar=variant.idvariante, cantidad=data.cantidad))
            record(db, "carrito_item_agregado", user_id, peer, True)
        else:
            nuevo = detail.cantidad + data.cantidad
            if nuevo > comercio_repo.disponibilidad_global(db, [variant.idvariante]).get(variant.idvariante, 0):
                raise DomainError(409, "disponibilidad_insuficiente",
                                  "No hay disponibilidad suficiente para esta prenda")
            detail.cantidad = nuevo
            record(db, "carrito_item_actualizado", user_id, peer, True)
        return _construir(db, cart)


def modificar(db, idDetalle: int, data: ItemCantidad, user_id: str, peer):
    with transaction(db):
        _permitir_edicion(db, user_id)
        cart = repository.locked_active_cart(db, user_id)
        if cart is None:
            raise DomainError(404, "item_no_encontrado", "Producto no encontrado en el carrito")
        detail = repository.locked_detail(db, cart.idcarrito, idDetalle)
        if detail is None:
            raise DomainError(404, "item_no_encontrado", "Producto no encontrado en el carrito")
        variant, _ = _variante_activa(db, detail.idvar)
        if data.cantidad > comercio_repo.disponibilidad_global(db, [variant.idvariante]).get(variant.idvariante, 0):
            raise DomainError(409, "disponibilidad_insuficiente",
                              "No hay disponibilidad suficiente para esta prenda")
        detail.cantidad = data.cantidad
        record(db, "carrito_item_actualizado", user_id, peer, True)
        return _construir(db, cart)


def eliminar(db, idDetalle: int, user_id: str, peer):
    with transaction(db):
        _permitir_edicion(db, user_id)
        cart = repository.locked_active_cart(db, user_id)
        if cart is None:
            raise DomainError(404, "item_no_encontrado", "Producto no encontrado en el carrito")
        detail = repository.locked_detail(db, cart.idcarrito, idDetalle)
        if detail is None:
            raise DomainError(404, "item_no_encontrado", "Producto no encontrado en el carrito")
        repository.remove_detail(db, detail)
        record(db, "carrito_item_eliminado", user_id, peer, True)
        # El carrito vacío permanece activo; CU13 lo convertirá. Sin "abandonado" automático.
        return _construir(db, cart)
