from sqlalchemy import delete, func, select, text

from app.core.database import Base
from app.core.errors import DomainError
from app.modules.cliente_experiencia_compra.shared.models.catalogo import (
    Categoria, Marca, Coleccion, Temporada, TempColeccion, Talla, Color, Producto,
    VarianteProd, VarianteColor, Proveedor, Promocion,
)

GROUPS = {
    'tallas': (Talla, 'idtalla', 'descripcion'), 'colores': (Color, 'idcolor', 'descripcion'),
    'colecciones': (Coleccion, 'idcol', 'descripcion'), 'temporadas': (Temporada, 'idtemp', 'nombre'),
    'categorias': (Categoria, 'idcat', 'descripcion'), 'marcas': (Marca, 'idmarca', 'nombre'),
    'proveedores': (Proveedor, 'idprov', 'nombre'),
}


def group_info(group):
    if group not in GROUPS:
        raise DomainError(404, 'panel_no_encontrado', 'Panel no encontrado')
    return GROUPS[group]


def get(db, model, id, lock=False, reference=False):
    pk = list(model.__table__.primary_key)[0]
    query = select(model).where(pk == id).execution_options(populate_existing=True)
    if lock:
        query = query.with_for_update()
    elif reference:
        query = query.with_for_update(read=True, key_share=True)
    row = db.scalar(query)
    if row is None:
        raise DomainError(422 if reference else 404, 'registro_no_encontrado', 'El registro seleccionado no existe')
    return row


def serialize(db, group, row):
    _, pk, label = group_info(group)
    result = {'id': getattr(row, pk), label: getattr(row, label)}
    if group in {'marcas', 'temporadas'}:
        result['estado'] = row.estado
    if group == 'colores':
        result['hex'] = row.hex
    if group == 'temporadas':
        result.update(fechaIni=row.fechaini, fechaFin=row.fechafin)
    if group == 'colecciones':
        result['idTemps'] = list(db.scalars(select(TempColeccion.idtemp).where(TempColeccion.idcol == row.idcol)
                                           .order_by(TempColeccion.idtemp)))
    if group == 'proveedores':
        result.update(correo=row.correo, direccion=row.direccion, telefono=row.telefono)
    return result


def list_group(db, group, q, offset, limit):
    model, pk, label = group_info(group)
    query = select(model)
    if q:
        query = query.where(func.lower(getattr(model, label)).contains(q.lower(), autoescape=True))
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = db.scalars(query.order_by(getattr(model, label), getattr(model, pk)).offset(offset).limit(limit)).all()
    # Asociación cargada en lote para no hacer N+1 en el listado de colecciones.
    if group == 'colecciones':
        links = db.execute(select(TempColeccion).where(TempColeccion.idcol.in_([r.idcol for r in rows]))).scalars().all()
        items = [{'id': r.idcol, 'descripcion': r.descripcion,
                  'idTemps': sorted(x.idtemp for x in links if x.idcol == r.idcol)} for r in rows]
    else:
        items = [serialize(db, group, row) for row in rows]
    return {'items': items, 'total': total, 'offset': offset, 'limit': limit}


def allocate_smallint(db, model, pk):
    if db.get_bind().dialect.name == 'postgresql':
        db.execute(text(f'LOCK TABLE {model.__tablename__} IN SHARE ROW EXCLUSIVE MODE'))
    id = max(0, db.scalar(select(func.max(getattr(model, pk)))) or 0) + 1
    if id > 32767:
        raise DomainError(409, 'identificadores_agotados', 'Se alcanzó el límite de registros')
    return id


def prevent_cascade(db, row, allowed_tables=()):
    """Impide que ON DELETE CASCADE borre inventario, historial u otras referencias."""
    table = row.__table__
    for other in Base.metadata.tables.values():
        if other.name in allowed_tables:
            continue
        for foreign in other.foreign_keys:
            if foreign.column.table is table:
                value = getattr(row, foreign.column.key)
                if db.scalar(select(foreign.parent).where(foreign.parent == value).limit(1)) is not None:
                    raise DomainError(409, 'registro_en_uso', 'No se puede eliminar: tiene registros asociados. Desactívalo si corresponde.')


def product_detail(db, row):
    variants = db.scalars(select(VarianteProd).where(VarianteProd.idprod == row.idprod).order_by(VarianteProd.idvariante)).all()
    colors = db.execute(select(VarianteColor).where(VarianteColor.idvar.in_([v.idvariante for v in variants]))).scalars().all()
    return {'idProd': row.idprod, 'descripcion': row.descripcion, 'estado': row.estado,
            'idCat': row.idcat, 'idMarca': row.idmarca, 'idCol': row.idcol, 'idProv': row.idprov, 'idPromo': row.idpromo,
            'variantes': [{'idVariante': v.idvariante, 'sku': v.sku, 'precio': v.precio, 'estado': v.estado,
                          'img': v.img, 'idTalla': v.idtalla, 'idColores': sorted(c.idcolor for c in colors if c.idvar == v.idvariante)} for v in variants]}


def list_products(db, q, estado, offset, limit):
    query = select(Producto, Categoria.descripcion, Marca.nombre, Coleccion.descripcion, Proveedor.nombre).join(
        Categoria, Categoria.idcat == Producto.idcat).join(Marca, Marca.idmarca == Producto.idmarca).join(
        Coleccion, Coleccion.idcol == Producto.idcol).join(Proveedor, Proveedor.idprov == Producto.idprov)
    if q:
        query = query.where(func.lower(Producto.descripcion).contains(q.lower(), autoescape=True) |
                            select(VarianteProd.idvariante).where(VarianteProd.idprod == Producto.idprod,
                                func.lower(VarianteProd.sku).contains(q.lower(), autoescape=True)).exists())
    if estado:
        query = query.where(Producto.estado == estado)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = db.execute(query.order_by(Producto.descripcion, Producto.idprod).offset(offset).limit(limit)).all()
    counts = dict(db.execute(select(VarianteProd.idprod, func.count()).where(VarianteProd.idprod.in_([r[0].idprod for r in rows]))
                             .group_by(VarianteProd.idprod)).all())
    return {'items': [{'idProd': p.idprod, 'descripcion': p.descripcion, 'estado': p.estado,
                       'categoria': c, 'marca': m, 'coleccion': col, 'proveedor': prov,
                       'totalVariantes': counts.get(p.idprod, 0)} for p, c, m, col, prov in rows],
            'total': total, 'offset': offset, 'limit': limit}


def references(db, include_suppliers=True):
    result = {}
    for group, (model, pk, label) in GROUPS.items():
        if group == 'proveedores' and not include_suppliers:
            continue
        result[group] = [{'id': getattr(r, pk), 'nombre': getattr(r, label)} for r in
                         db.scalars(select(model).order_by(getattr(model, label), getattr(model, pk))).all()]
    links = db.scalars(select(TempColeccion)).all()
    for collection in result['colecciones']:
        collection['idTemps'] = sorted(link.idtemp for link in links if link.idcol == collection['id'])
    result['promociones'] = [{'id': r.idpromo, 'nombre': r.nombre} for r in db.scalars(select(Promocion).order_by(Promocion.nombre)).all()]
    return result
