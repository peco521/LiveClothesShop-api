from datetime import date
from sqlalchemy import func, select
from app.core.errors import DomainError
from app.modules.cliente_experiencia_compra.shared.models.catalogo import Producto, Promocion
from app.modules.inventario_productos.shared.access import transaction
from app.modules.seguridad_accesos.services.bitacora import record


def view(db, row):
    today = date.today()
    status = 'cancelada' if row.estado != 'activo' else 'programada' if today < row.fechaini else 'finalizada' if today > row.fechafin else 'activa'
    return dict(idPromo=row.idpromo, nombre=row.nombre, descripcion=row.descripcion,
                tipoDescuento=row.tipodescuento, valorDescuento=row.valordescuento,
                fechaIni=row.fechaini, fechaFin=row.fechafin, estado=status,
                productos=list(db.scalars(select(Producto.idprod).where(Producto.idpromo == row.idpromo))))


def listing(db, q, offset, limit):
    conditions = [Promocion.nombre.ilike('%' + q + '%')] if q else []
    rows = db.scalars(select(Promocion).where(*conditions).order_by(Promocion.idpromo.desc()).offset(offset).limit(limit))
    return dict(items=[view(db, r) for r in rows], total=db.scalar(select(func.count()).select_from(Promocion).where(*conditions)), offset=offset, limit=limit)


def save(db, data, actor, peer, nro=None):
    with transaction(db):
        # Lock product associations before promotions to serialize competing assignments.
        current = list(db.scalars(select(Producto).where(Producto.idpromo == nro))) if nro else []
        ids = sorted(set(data.productos) | {p.idprod for p in current})
        products = {p.idprod: p for p in db.scalars(select(Producto).where(Producto.idprod.in_(ids)).order_by(Producto.idprod).with_for_update().execution_options(populate_existing=True))}
        if any(key not in products or products[key].estado != 'activo' for key in data.productos):
            raise DomainError(404, 'producto_no_encontrado', 'Una prenda seleccionada no está disponible')
        row = db.scalar(select(Promocion).where(Promocion.idpromo == nro).with_for_update()) if nro else None
        if nro and row is None:
            raise DomainError(404, 'promocion_no_encontrada', 'Promoción no encontrada')
        if row and row.estado != 'activo':
            raise DomainError(409, 'promocion_cancelada', 'Una promoción desactivada no puede volver a editarse')
        others = {r.idpromo: r for r in db.scalars(select(Promocion).where(Promocion.idpromo.in_({p.idpromo for p in products.values() if p.idpromo is not None})))}
        for key in data.productos:
            previous = others.get(products[key].idpromo)
            if previous and previous.idpromo != nro and previous.estado == 'activo' and previous.fechafin >= date.today():
                # Schema has one FK/product: never overwrite another scheduled or active promotion.
                raise DomainError(409, 'promocion_superpuesta', 'Una prenda ya tiene otra promoción activa o programada')
        if row is None:
            row = Promocion(estado='activo')
            db.add(row)
        row.nombre, row.descripcion = data.nombre, data.descripcion
        row.tipodescuento, row.valordescuento = data.tipoDescuento, data.valorDescuento
        row.fechaini, row.fechafin = data.fechaIni, data.fechaFin
        db.flush()
        for p in products.values():
            if p.idprod in data.productos:
                p.idpromo = row.idpromo
            elif p.idpromo == row.idpromo:
                p.idpromo = None
        db.flush()
        record(db, 'promocion_guardada', actor, peer, True)
        return view(db, row)


def deactivate(db, nro, actor, peer):
    with transaction(db):
        row = db.scalar(select(Promocion).where(Promocion.idpromo == nro).with_for_update())
        if row is None:
            raise DomainError(404, 'promocion_no_encontrada', 'Promoción no encontrada')
        if row.estado == 'activo':
            row.estado = 'inactivo'
            record(db, 'promocion_desactivada', actor, peer, True)
        return view(db, row)
