"""CU13 Compra digital: carrito activo → venta registrada pendiente de pago.

NO descuenta inventario, NO crea movimientos, NO convierte el carrito.
Eso pertenece a CU14 (pago aprobado). Aquí solo se congela la foto económica.
"""

from contextlib import contextmanager
from decimal import Decimal

from sqlalchemy.exc import IntegrityError

from app.core.errors import DomainError
from app.modules.cliente_experiencia_compra.cu11_gestionar_reserva.repositories import reserva as reserva_repo
from app.modules.cliente_experiencia_compra.cu12_carrito.repositories import carrito as carrito_repo
from app.modules.cliente_experiencia_compra.cu13_compra_digital.repositories import venta as repository
from app.modules.cliente_experiencia_compra.cu13_compra_digital.schemas.compra import (
    CompraCrear,
    VentaDetalle,
    VentaItem,
    VentaSucursal,
)
from app.modules.cliente_experiencia_compra.shared.models.comercio import DetalleVenta, Venta
from app.modules.cliente_experiencia_compra.shared.repositories import catalogo as catalogo_repo
from app.modules.cliente_experiencia_compra.shared.repositories import comercio as comercio_repo
from app.modules.cliente_experiencia_compra.shared.services.precios import (
    descuento_unitario,
    moneda,
    promocion_vigente,
)
from app.modules.seguridad_accesos.services.bitacora import record
from app.modules.seguridad_accesos.shared.repositories import organizacion


@contextmanager
def transaction(db):
    # require_cliente ya abrió esta transacción en la sesión compartida.
    try:
        yield
        db.commit()
    except IntegrityError:
        db.rollback()
        raise DomainError(409, "conflicto_integridad", "Los datos entran en conflicto con un registro existente") from None
    except Exception:
        db.rollback()
        raise


def _vista(db, row: Venta):
    # Histórico congelado: precioUnitario/cantidad por línea y descAplicado/total
    # de la venta. El descuento por línea NO se persiste ni se reconstruye con
    # la promoción vigente actual (podría haber cambiado).
    detalles = repository.detalles(db, row.nroventa)
    branch = organizacion.branch(db, row.nrosuc)
    city = organizacion.city(db, branch.idciud) if branch else None
    found = reserva_repo.variantes_productos(db, sorted({d.idvar for d in detalles}))
    items = []
    for detail in detalles:
        pair = found.get(detail.idvar)
        variant, product = pair if pair else (None, None)
        bruto = moneda(detail.preciounitario * detail.cantidad)
        items.append(VentaItem(
            idDetalleVenta=detail.iddetalleventa, idVar=detail.idvar,
            sku=variant.sku if variant else detail.idvar,
            producto=product.descripcion if product else detail.idvar,
            cantidad=detail.cantidad, precioUnitario=detail.preciounitario,
            subtotalBruto=bruto))
    return VentaDetalle(
        nroVenta=row.nroventa, fechaHora=row.fechahora, estado=row.estado, nit=row.nit,
        sucursal=VentaSucursal(nro=row.nrosuc, nombre=branch.nombre if branch else str(row.nrosuc),
                               ciudad=city.nombre if city else ""),
        carrito=row.idcarrito, items=items,
        brutoTotal=sum((i.subtotalBruto for i in items), Decimal("0")),
        descAplicado=row.desc_aplicado, total=row.total)


def checkout(db, data: CompraCrear, user_id: str, peer):
    """Prepara la venta. Retorna (vista, creada): idempotente ante reintentos."""
    with transaction(db):
        if carrito_repo.locked_cliente(db, user_id) is None:
            raise DomainError(403, "acceso_denegado", "No tiene autorización para esta operación")
        cart = carrito_repo.locked_active_cart(db, user_id)
        if cart is None:
            raise DomainError(409, "carrito_no_disponible", "No tienes un carrito activo para comprar")
        detalles_carro = carrito_repo.cart_details(db, cart.idcarrito)
        if not detalles_carro:
            raise DomainError(409, "carrito_no_disponible", "No tienes un carrito activo para comprar")
        existente = repository.locked_registrada_por_carrito(db, cart.idcarrito)
        if existente is not None:
            if existente.nrosuc != data.nroSuc or existente.nit != data.nit:
                raise DomainError(409, "compra_pendiente", "Cancela la compra pendiente antes de cambiar sus datos")
            actual = sorted((d.idvar, d.cantidad) for d in detalles_carro)
            congelado = sorted((d.idvar, d.cantidad) for d in repository.detalles(db, existente.nroventa))
            if actual != congelado:
                raise DomainError(409, "carrito_modificado", "El carrito cambió; cancela la compra pendiente")
            # Doble checkout: se reutiliza la venta pendiente, no se duplica.
            return _vista(db, existente), False
        branch = organizacion.branch(db, data.nroSuc, lock=True)
        if branch is None:
            raise DomainError(404, "sucursal_no_encontrada", "Sucursal no encontrada")
        if branch.estado != "activo":
            raise DomainError(409, "sucursal_inactiva", "La sucursal no está disponible")
        var_ids = sorted({d.idvar for d in detalles_carro})
        found = reserva_repo.variantes_productos(db, var_ids)
        promos = catalogo_repo.promociones(db, [p.idpromo for _, p in found.values()])
        lineas = []
        bruto_total = Decimal("0")
        desc_total = Decimal("0")
        for detail in sorted(detalles_carro, key=lambda d: d.idvar):
            pair = found.get(detail.idvar)
            if pair is None:
                raise DomainError(404, "variante_no_encontrada", "Variante no encontrada")
            variant, product = pair
            if variant.estado != "activo":
                raise DomainError(404, "variante_no_encontrada", "Variante no encontrada")
            if product.estado != "activo":
                raise DomainError(404, "producto_no_encontrado", "Prenda no encontrada")
            # Disponibilidad REAL de la sucursal elegida (precondición, sin descontar).
            filas = comercio_repo.locked_inventarios(db, detail.idvar, branch.nro)
            if sum(r.cantdisp for r in filas) < detail.cantidad:
                raise DomainError(409, "disponibilidad_insuficiente",
                                  "No hay disponibilidad suficiente en la sucursal elegida")
            base = moneda(variant.precio)
            promo = promos.get(product.idpromo)
            desc_unidad = descuento_unitario(base, promo)
            bruto = moneda(base * detail.cantidad)
            descuento = moneda(desc_unidad * detail.cantidad)
            bruto_total += bruto
            desc_total += descuento
            lineas.append((detail, base, desc_unidad))
        venta = repository.add_venta(db, Venta(
            nit=data.nit, total=moneda(bruto_total - desc_total), desc_aplicado=moneda(desc_total),
            estado="registrada", idusuariocl=user_id, idusuarioemp=None,
            nrosuc=branch.nro, idcarrito=cart.idcarrito, nroreserva=None))
        for position, (detail, base, descuento) in enumerate(lineas, start=1):
            repository.add_detalle(db, DetalleVenta(
                nroventa=venta.nroventa, iddetalleventa=position,
                preciounitario=base, descuentounitario=descuento, cantidad=detail.cantidad, idvar=detail.idvar))
        record(db, "venta_registrada", user_id, peer, True)
        # Inventario intacto, carrito activo: el cierre lo hace CU14.
        return _vista(db, venta), True


def detalle(db, nroVenta: int, user_id: str):
    row = repository.propia(db, nroVenta, user_id)
    if row is None:
        raise DomainError(404, "venta_no_encontrada", "Venta no encontrada")
    return _vista(db, row)
