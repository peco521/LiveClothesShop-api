"""Consultas de lectura del catálogo (CU10, solo lectura, sin N+1)."""

from sqlalchemy import func, or_, select

from app.modules.cliente_experiencia_compra.shared.models.catalogo import (
    Categoria,
    Coleccion,
    Color,
    Inventario,
    Marca,
    Producto,
    Promocion,
    Talla,
    TempColeccion,
    Temporada,
    VarianteColor,
    VarianteProd,
)
from app.modules.seguridad_accesos.shared.models import Ciudad, Sucursal


def _variant_scope(product_col=None):
    """Condición base: variante activa del producto dado (o correlacionada)."""
    condition = VarianteProd.estado == "activo"
    if product_col is not None:
        condition = condition & (VarianteProd.idprod == product_col)
    return condition


def _price_exists(min_precio=None, max_precio=None):
    query = select(VarianteProd.idvariante).where(
        VarianteProd.idprod == Producto.idprod, VarianteProd.estado == "activo")
    if min_precio is not None:
        query = query.where(VarianteProd.precio >= min_precio)
    if max_precio is not None:
        query = query.where(VarianteProd.precio <= max_precio)
    return query.exists()


def _availability_exists():
    return (select(Inventario.nroinv)
            .join(VarianteProd, VarianteProd.idvariante == Inventario.idvar)
            .join(Sucursal, Sucursal.nro == Inventario.nrosuc)
            .where(VarianteProd.idprod == Producto.idprod,
                   VarianteProd.estado == "activo",
                   Sucursal.estado == "activo",
                   Inventario.cantdisp > 0)
            .exists())


def list_products(db, *, q="", idCat=None, idMarca=None, idCol=None, idTemp=None,
                  idTalla=None, idColor=None, minPrecio=None, maxPrecio=None,
                  soloDisponibles=False, offset=0, limit=20, sort="nombre_asc"):
    conditions = [Producto.estado == "activo"]
    if q:
        like = f"%{q}%"
        conditions.append(or_(Producto.descripcion.ilike(like),
                              Producto.idprod.ilike(like),
                              Marca.nombre.ilike(like),
                              Categoria.descripcion.ilike(like)))
    if idCat is not None:
        conditions.append(Producto.idcat == idCat)
    if idMarca is not None:
        conditions.append(Producto.idmarca == idMarca)
    if idCol is not None:
        conditions.append(Producto.idcol == idCol)
    if idTemp is not None:
        conditions.append(Producto.idcol.in_(
            select(TempColeccion.idcol).where(TempColeccion.idtemp == idTemp)))
    if idTalla is not None:
        conditions.append(select(VarianteProd.idvariante).where(
            VarianteProd.idprod == Producto.idprod,
            VarianteProd.estado == "activo",
            VarianteProd.idtalla == idTalla).exists())
    if idColor is not None:
        conditions.append(select(VarianteColor.idvar).join(
            VarianteProd, VarianteProd.idvariante == VarianteColor.idvar).where(
            VarianteProd.idprod == Producto.idprod,
            VarianteProd.estado == "activo",
            VarianteColor.idcolor == idColor).exists())
    if minPrecio is not None or maxPrecio is not None:
        conditions.append(_price_exists(minPrecio, maxPrecio))
    if soloDisponibles:
        conditions.append(_availability_exists())

    base = (select(Producto, Marca, Categoria, Coleccion, Promocion)
            .join(Marca, Marca.idmarca == Producto.idmarca)
            .join(Categoria, Categoria.idcat == Producto.idcat)
            .join(Coleccion, Coleccion.idcol == Producto.idcol)
            .outerjoin(Promocion, Promocion.idpromo == Producto.idpromo)
            .where(*conditions))

    total = db.scalar(select(func.count()).select_from(Producto)
                      .join(Marca, Marca.idmarca == Producto.idmarca)
                      .join(Categoria, Categoria.idcat == Producto.idcat)
                      .where(*conditions)) or 0

    price_min = (select(func.min(VarianteProd.precio))
                 .where(VarianteProd.idprod == Producto.idprod,
                        VarianteProd.estado == "activo")
                 .correlate(Producto).scalar_subquery())
    if sort == "precio_asc":
        base = base.order_by(price_min.asc().nulls_last(), Producto.idprod)
    elif sort == "precio_desc":
        base = base.order_by(price_min.desc().nulls_last(), Producto.idprod)
    elif sort == "nombre_desc":
        base = base.order_by(Producto.descripcion.desc(), Producto.idprod)
    else:
        base = base.order_by(Producto.descripcion.asc(), Producto.idprod)

    rows = list(db.execute(base.offset(offset).limit(limit)).all())
    return rows, total


def product_variants(db, product_ids):
    if not product_ids:
        return []
    return list(db.scalars(select(VarianteProd)
                           .where(VarianteProd.idprod.in_(product_ids),
                                  VarianteProd.estado == "activo")
                           .order_by(VarianteProd.precio, VarianteProd.idvariante)).all())


def variant_colors(db, variant_ids):
    if not variant_ids:
        return {}
    rows = db.execute(select(VarianteColor.idvar, Color)
                      .join(Color, Color.idcolor == VarianteColor.idcolor)
                      .where(VarianteColor.idvar.in_(variant_ids))
                      .order_by(Color.descripcion)).all()
    grouped: dict[str, list] = {}
    for var_id, color in rows:
        grouped.setdefault(var_id, []).append(color)
    return grouped


def tallas_map(db, talla_ids):
    if not talla_ids:
        return {}
    rows = db.scalars(select(Talla).where(Talla.idtalla.in_(talla_ids))).all()
    return {row.idtalla: row for row in rows}


def availability_totals(db, product_ids):
    """Suma de cantDisp por producto (solo sucursales activas y variantes activas)."""
    if not product_ids:
        return {}
    rows = db.execute(select(VarianteProd.idprod, func.coalesce(func.sum(Inventario.cantdisp), 0))
                      .outerjoin(Inventario, (Inventario.idvar == VarianteProd.idvariante))
                      .outerjoin(Sucursal, (Sucursal.nro == Inventario.nrosuc) & (Sucursal.estado == "activo"))
                      .where(VarianteProd.idprod.in_(product_ids),
                             VarianteProd.estado == "activo")
                      .group_by(VarianteProd.idprod)).all()
    return {pid: int(total or 0) for pid, total in rows}


def get_product(db, idProd):
    return db.execute(select(Producto, Marca, Categoria, Coleccion, Promocion)
                      .join(Marca, Marca.idmarca == Producto.idmarca)
                      .join(Categoria, Categoria.idcat == Producto.idcat)
                      .join(Coleccion, Coleccion.idcol == Producto.idcol)
                      .outerjoin(Promocion, Promocion.idpromo == Producto.idpromo)
                      .where(Producto.idprod == idProd)).first()


def availability_by_branch(db, variant_ids):
    if not variant_ids:
        return []
    return list(db.execute(select(Inventario, Sucursal, Ciudad)
                           .join(Sucursal, Sucursal.nro == Inventario.nrosuc)
                           .join(Ciudad, Ciudad.id == Sucursal.idciud)
                           .where(Inventario.idvar.in_(variant_ids),
                                  Sucursal.estado == "activo")
                           .order_by(Ciudad.nombre, Sucursal.nombre, Inventario.idvar)).all())


def category_brands(db, category_id):
    """Marcas activas vinculadas a prendas activas de la categoría, sin duplicados."""
    return list(db.scalars(select(Marca).where(
        Marca.estado == "activo",
        select(Producto.idprod).where(
            Producto.idmarca == Marca.idmarca,
            Producto.idcat == category_id,
            Producto.estado == "activo").exists(),
    ).order_by(Marca.nombre, Marca.idmarca)).all())


def facets(db):
    """Listas de referencia para los filtros del catálogo (solo lectura)."""
    return {
        "categorias": list(db.scalars(select(Categoria).order_by(Categoria.descripcion, Categoria.idcat)).all()),
        "marcas": list(db.scalars(select(Marca).where(Marca.estado == "activo")
                                  .order_by(Marca.nombre, Marca.idmarca)).all()),
        "colecciones": list(db.scalars(select(Coleccion).order_by(Coleccion.descripcion, Coleccion.idcol)).all()),
        "temporadas": list(db.scalars(select(Temporada).order_by(Temporada.nombre, Temporada.idtemp)).all()),
        "tallas": list(db.scalars(select(Talla).order_by(Talla.idtalla)).all()),
        "colores": list(db.scalars(select(Color).order_by(Color.descripcion, Color.idcolor)).all()),
    }


def promociones(db, ids):
    """Promociones por id (lote único, sin N+1)."""
    ids = [i for i in set(ids) if i is not None]
    if not ids:
        return {}
    rows = db.scalars(select(Promocion).where(Promocion.idpromo.in_(ids))).all()
    return {row.idpromo: row for row in rows}
def get_variant(db, id_var: str):
    return db.scalar(select(VarianteProd).where(
        VarianteProd.idvariante == id_var, VarianteProd.estado == "activo"))
