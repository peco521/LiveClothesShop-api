"""CU10 Consultar Prendas: servicio de solo lectura."""

from app.core.errors import DomainError
from app.modules.cliente_experiencia_compra.shared.services.precios import promocion_vigente
from app.modules.cliente_experiencia_compra.cu10_consultar_prendas.schemas.catalogo import (
    CatalogoFiltros,
    ColeccionResumen,
    ColorDetalle,
    DisponibilidadSucursal,
    FacetaItem,
    FacetasListado,
    MarcaResumen,
    CategoriaResumen,
    ProductoDetalle,
    ProductoResumen,
    ProductosListado,
    PromocionResumen,
    TallaDetalle,
    VarianteDetalle,
)
from app.modules.cliente_experiencia_compra.shared.repositories import catalogo as repository


def _promo_resumen(promo):
    if not promocion_vigente(promo):
        return None
    return PromocionResumen(idPromo=promo.idpromo, nombre=promo.nombre,
                            tipoDescuento=promo.tipodescuento,
                            valorDescuento=promo.valordescuento)


def list_products(db, filters: CatalogoFiltros):
    if (filters.minPrecio is not None and filters.maxPrecio is not None
            and filters.minPrecio > filters.maxPrecio):
        raise DomainError(422, "datos_invalidos", "Los datos enviados no son válidos")
    rows, total = repository.list_products(
        db, q=filters.q, idCat=filters.idCat, idMarca=filters.idMarca,
        idCol=filters.idCol, idTemp=filters.idTemp, idTalla=filters.idTalla,
        idColor=filters.idColor, minPrecio=filters.minPrecio,
        maxPrecio=filters.maxPrecio, soloDisponibles=filters.soloDisponibles,
        offset=filters.offset, limit=filters.limit, sort=filters.sort)
    product_ids = [product.idprod for product, *_ in rows]
    variants = repository.product_variants(db, product_ids)
    by_product: dict[str, list] = {}
    for variant in variants:
        by_product.setdefault(variant.idprod, []).append(variant)
    totals = repository.availability_totals(db, product_ids)

    items = []
    for product, marca, categoria, coleccion, promo in rows:
        product_variants = by_product.get(product.idprod, [])
        prices = [variant.precio for variant in product_variants]
        first_image = next((variant.img for variant in product_variants if variant.img), None)
        items.append(ProductoResumen(
            idProd=product.idprod, descripcion=product.descripcion,
            categoria=CategoriaResumen(idCat=categoria.idcat, descripcion=categoria.descripcion),
            marca=MarcaResumen(idMarca=marca.idmarca, nombre=marca.nombre),
            coleccion=ColeccionResumen(idCol=coleccion.idcol, descripcion=coleccion.descripcion),
            promocion=_promo_resumen(promo),
            precioMin=min(prices) if prices else None,
            precioMax=max(prices) if prices else None,
            imagen=first_image,
            disponible=(totals.get(product.idprod, 0) > 0),
            totalVariantes=len(product_variants)))
    return ProductosListado(items=items, total=total, offset=filters.offset, limit=filters.limit)


def variant_product_detail(db, idVar: str):
    variant = repository.get_variant(db, idVar.strip())
    if variant is None:
        raise DomainError(404, "variante_no_encontrada", "La prenda seleccionada ya no está disponible")
    return product_detail(db, variant.idprod)


def product_detail(db, idProd: str):
    found = repository.get_product(db, idProd.strip())
    if found is None:
        raise DomainError(404, "producto_no_encontrado", "Prenda no encontrada")
    product, marca, categoria, coleccion, promo = found
    if product.estado != "activo":
        raise DomainError(404, "producto_no_encontrado", "Prenda no encontrada")
    variants = repository.product_variants(db, [product.idprod])
    variant_ids = [variant.idvariante for variant in variants]
    colors = repository.variant_colors(db, variant_ids)
    tallas = repository.tallas_map(db, list({variant.idtalla for variant in variants}))
    availability = repository.availability_by_branch(db, variant_ids)

    variant_rows = []
    for variant in variants:
        talla = tallas.get(variant.idtalla)
        variant_rows.append(VarianteDetalle(
            idVariante=variant.idvariante, sku=variant.sku, precio=variant.precio,
            imagen=variant.img,
            talla=TallaDetalle(idTalla=talla.idtalla, descripcion=talla.descripcion) if talla else TallaDetalle(
                idTalla=variant.idtalla, descripcion=str(variant.idtalla)),
            colores=[ColorDetalle(idColor=color.idcolor, descripcion=color.descripcion, hex=color.hex)
                     for color in colors.get(variant.idvariante, [])]))
    availability_rows = [DisponibilidadSucursal(
        nroSuc=sucursal.nro, sucursal=sucursal.nombre, ciudad=ciudad.nombre,
        idVariante=inventory.idvar, stock=inventory.stock, cantDisp=inventory.cantdisp)
        for inventory, sucursal, ciudad in availability]
    return ProductoDetalle(
        idProd=product.idprod, descripcion=product.descripcion, estado=product.estado,
        categoria=CategoriaResumen(idCat=categoria.idcat, descripcion=categoria.descripcion),
        marca=MarcaResumen(idMarca=marca.idmarca, nombre=marca.nombre),
        coleccion=ColeccionResumen(idCol=coleccion.idcol, descripcion=coleccion.descripcion),
        promocion=_promo_resumen(promo),
        variantes=variant_rows, disponibilidad=availability_rows)


def facets(db, group: str, *, idCat: int | None = None):
    if group == "marcas" and idCat is not None:
        rows = repository.category_brands(db, idCat)
        items = [FacetaItem(id=row.idmarca, nombre=row.nombre) for row in rows]
        return FacetasListado(items=items, total=len(items))
    data = repository.facets(db)
    if group not in data:
        raise DomainError(404, "faceta_no_encontrada", "Filtro no encontrado")
    rows = data[group]
    if group == "categorias":
        items = [FacetaItem(id=row.idcat, nombre=row.descripcion) for row in rows]
    elif group == "marcas":
        items = [FacetaItem(id=row.idmarca, nombre=row.nombre) for row in rows]
    elif group == "colecciones":
        items = [FacetaItem(id=row.idcol, nombre=row.descripcion) for row in rows]
    elif group == "temporadas":
        items = [FacetaItem(id=row.idtemp, nombre=row.nombre) for row in rows]
    elif group == "tallas":
        items = [FacetaItem(id=row.idtalla, nombre=row.descripcion) for row in rows]
    else:
        items = [FacetaItem(id=row.idcolor, nombre=row.descripcion) for row in rows]
    return FacetasListado(items=items, total=len(items))
