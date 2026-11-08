from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from app.core.errors import DomainError

from app.modules.cliente_experiencia_compra.shared.models.catalogo import (
    Inventario, VarianteProd, VarianteColor, Producto, Categoria, Talla, Color,
)
from app.modules.cliente_experiencia_compra.shared.models.comercio import MovimientoInv
from app.modules.seguridad_accesos.shared.models import Sucursal
from app.modules.inventario_productos.shared.tenant import select_for, tenant_id


def listing(db, scope, *, q='', nroSuc=None, idCat=None, idTalla=None, idColor=None, offset=0, limit=20):
    query = select_for(db, Inventario, VarianteProd, Producto, Sucursal.nombre, Categoria.descripcion, Talla.descripcion).join(
        VarianteProd, VarianteProd.idvariante == Inventario.idvar).join(Producto, Producto.idprod == VarianteProd.idprod).join(
        Sucursal, Sucursal.nro == Inventario.nrosuc).join(Categoria, Categoria.idcat == Producto.idcat).join(Talla, Talla.idtalla == VarianteProd.idtalla)
    if scope is not None:
        query = query.where(Inventario.nrosuc == scope)
    if nroSuc is not None:
        query = query.where(Inventario.nrosuc == nroSuc)
    if q:
        query = query.where(func.lower(Producto.descripcion).contains(q.lower(), autoescape=True) |
                            func.lower(VarianteProd.sku).contains(q.lower(), autoescape=True))
    if idCat is not None:
        query = query.where(Producto.idcat == idCat)
    if idTalla is not None:
        query = query.where(VarianteProd.idtalla == idTalla)
    if idColor is not None:
        query = query.where(select_for(db, VarianteColor.idvar).where(VarianteColor.idvar == VarianteProd.idvariante,
                                                           VarianteColor.idcolor == idColor).exists())
    total = db.scalar(select_for(db, func.count()).select_from(query.subquery())) or 0
    rows = db.execute(query.order_by(Sucursal.nombre, Producto.descripcion, VarianteProd.sku, Inventario.nroinv).offset(offset).limit(limit)).all()
    colors = db.execute(select_for(db, VarianteColor.idvar, Color.descripcion).join(Color, Color.idcolor == VarianteColor.idcolor)
                        .where(VarianteColor.idvar.in_([r[1].idvariante for r in rows])).order_by(Color.descripcion)).all()
    return {'items': [{'nroInv': inv.nroinv, 'nroSuc': inv.nrosuc, 'sucursal': suc, 'idVariante': variant.idvariante,
                       'sku': variant.sku, 'descripcion': prod.descripcion, 'categoria': cat, 'talla': talla,
                       'colores': [name for id, name in colors if id == variant.idvariante],
                       'stock': inv.stock, 'cantDisp': inv.cantdisp, 'reservado': inv.stock - inv.cantdisp}
                      for inv, variant, prod, suc, cat, talla in rows], 'total': total, 'offset': offset, 'limit': limit}


def references(db, scope):
    branches = select_for(db, Sucursal).order_by(Sucursal.nombre, Sucursal.nro)
    if scope is not None:
        branches = branches.where(Sucursal.nro == scope)
    variants = db.execute(select_for(db, VarianteProd, Producto.descripcion, Talla.descripcion).join(Producto, Producto.idprod == VarianteProd.idprod)
                          .join(Talla, Talla.idtalla == VarianteProd.idtalla).order_by(Producto.descripcion, VarianteProd.sku)).all()
    return {'sucursales': [{'id': r.nro, 'nombre': r.nombre} for r in db.scalars(branches)],
            'variantes': [{'id': v.idvariante, 'nombre': f'{name} · {talla} · {v.sku}'} for v, name, talla in variants],
            'categorias': [{'id': r.idcat, 'nombre': r.descripcion} for r in db.scalars(select_for(db, Categoria).order_by(Categoria.descripcion))],
            'tallas': [{'id': r.idtalla, 'nombre': r.descripcion} for r in db.scalars(select_for(db, Talla).order_by(Talla.descripcion))],
            'colores': [{'id': r.idcolor, 'nombre': r.descripcion} for r in db.scalars(select_for(db, Color).order_by(Color.descripcion))],
            'sucursalAsignada': scope}


def inventory_row(db, branch, variant):
    # Coordina altas concurrentes de la misma pareja; después se bloquea la fila real.
    if db.get_bind().dialect.name == 'postgresql':
        db.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))'), {'key': f'CU20:{tenant_id(db)}:{branch}:{variant}'})
    return db.scalar(select_for(db, Inventario).where(Inventario.nrosuc == branch, Inventario.idvar == variant)
                     .with_for_update().execution_options(populate_existing=True))


def call_movement(db, id, tipo, cantidad, motivo):
    tenant = tenant_id(db)
    if tenant is not None:
        for attempt in range(512):
            try:
                with db.begin_nested():
                    db.execute(text('CALL sp_registrar_movimiento_inventario(:tenant, :id, CAST(:tipo AS varchar), :cantidad, CAST(:motivo AS varchar))'),
                               {'tenant': tenant, 'id': id, 'tipo': tipo, 'cantidad': cantidad, 'motivo': motivo})
                break
            except IntegrityError as error:
                if getattr(getattr(error.orig, 'diag', None), 'constraint_name', None) != 'movimiento_inv_pkey':
                    raise
        else:
            raise DomainError(409,'secuencia_no_disponible','No se pudo asignar un identificador de movimiento')
        return
    db.execute(text('CALL sp_registrar_movimiento_inventario(:id, CAST(:tipo AS varchar), :cantidad, CAST(:motivo AS varchar))'),
               {'id': id, 'tipo': tipo, 'cantidad': cantidad, 'motivo': motivo})


def create_inventory(db, branch, variant):
    """Consume stale identity values safely, without resetting a shared sequence.

    Explicit historical seed IDs may be ahead of nextval. A failed insert is
    rolled back to a savepoint; only the PK collision is retried. RLS/grants and
    other tenants' rows are never bypassed, read, updated or deleted.
    """
    for attempt in range(512):
        try:
            with db.begin_nested():
                row=Inventario(nrosuc=branch,idvar=variant,stock=0,cantdisp=0)
                if tenant_id(db) is not None: row.idemp=tenant_id(db)
                db.add(row)
                db.flush()
            return row
        except IntegrityError as error:
            if getattr(getattr(error.orig, 'diag', None), 'constraint_name', None) != 'inventario_pkey':
                raise
    raise DomainError(409,'secuencia_no_disponible','No se pudo asignar un identificador de inventario')


def movement_detail(row):
    return {'idMov': row.idmov, 'tipoMov': row.tipomov, 'cantidad': row.cantidad, 'fecha': row.fecha, 'motivo': row.motivo, 'nroInv': row.nroinv}


def history(db, id, offset, limit):
    # Scope the movement and its owning inventory; RLS is additional protection.
    query = select_for(db, MovimientoInv).join(Inventario, Inventario.nroinv == MovimientoInv.nroinv).where(MovimientoInv.nroinv == id)
    total = db.scalar(select_for(db, func.count()).select_from(query.subquery())) or 0
    rows = db.scalars(query.order_by(MovimientoInv.idmov.desc()).offset(offset).limit(limit)).all()
    return {'items': [movement_detail(r) for r in rows], 'total': total, 'offset': offset, 'limit': limit}
