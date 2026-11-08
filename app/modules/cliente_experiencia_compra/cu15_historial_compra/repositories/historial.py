"""Consultas de solo lectura para el historial de compras del cliente."""

from sqlalchemy import func, select

from app.modules.cliente_experiencia_compra.shared.models.catalogo import Producto, VarianteProd
from app.modules.cliente_experiencia_compra.shared.models.comercio import DetalleVenta, Pago, Venta
from app.modules.seguridad_accesos.shared.models import Ciudad, Sucursal
from app.modules.cliente_experiencia_compra.shared.repositories.tenant import scoped


def listar_ventas(db, user_id: str, offset: int, limit: int, *, branch_id=None):
    condicion = Venta.idusuariocl == user_id
    if branch_id is not None:
        condicion = condicion & (Venta.nrosuc == branch_id)
    total = db.scalar(scoped(db,select(func.count()).select_from(Venta),Venta).where(condicion)) or 0
    ventas = list(db.scalars(
        scoped(db,select(Venta),Venta).where(condicion)
        .order_by(Venta.fechahora.desc(), Venta.nroventa.desc())
        .offset(offset).limit(limit)
    ).all())
    return ventas, total


def venta_propia(db, nro_venta: int, user_id: str, *, branch_id=None):
    query = scoped(db,select(Venta),Venta).where(
        Venta.nroventa == nro_venta,
        Venta.idusuariocl == user_id,
    )
    if branch_id is not None:
        query = query.where(Venta.nrosuc == branch_id)
    return db.scalar(query)


def detalles_de_ventas(db, numeros: list[int]):
    if not numeros:
        return []
    return db.execute(
        scoped(db,select(
            DetalleVenta.nroventa,
            DetalleVenta.iddetalleventa,
            DetalleVenta.idvar,
            DetalleVenta.cantidad,
            DetalleVenta.preciounitario,
            VarianteProd.sku,
            Producto.descripcion.label("producto"),
        ),DetalleVenta,VarianteProd,Producto)
        .outerjoin(VarianteProd, VarianteProd.idvariante == DetalleVenta.idvar)
        .outerjoin(Producto, Producto.idprod == VarianteProd.idprod)
        .where(DetalleVenta.nroventa.in_(numeros))
        .order_by(DetalleVenta.nroventa, DetalleVenta.iddetalleventa)
    ).all()


def pagos_de_ventas(db, numeros: list[int]):
    if not numeros:
        return []
    return list(db.scalars(
        scoped(db,select(Pago),Pago).where(Pago.nroventa.in_(numeros))
        .order_by(Pago.nroventa, Pago.idpago)
    ).all())


def sucursales(db, numeros: list[int]):
    if not numeros:
        return {}
    rows = db.execute(
        scoped(db,select(Sucursal, Ciudad),Sucursal,Ciudad)
        .outerjoin(Ciudad, Ciudad.id == Sucursal.idciud)
        .where(Sucursal.nro.in_(numeros))
    ).all()
    return {sucursal.nro: (sucursal, ciudad) for sucursal, ciudad in rows}
