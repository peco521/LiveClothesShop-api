from secrets import token_hex

from pydantic import ValidationError
from sqlalchemy import delete, select

from app.core.errors import DomainError
from app.modules.inventario_productos.shared.access import actor_scope, transaction
from app.modules.inventario_productos.cu18_gestionar_catalogo.schemas import catalogo as schema
from app.modules.inventario_productos.cu18_gestionar_catalogo.repositories import catalogo as repo
from app.modules.cliente_experiencia_compra.shared.models.catalogo import (
    Categoria, Marca, Coleccion, Temporada, TempColeccion, Talla, Color, Producto, VarianteProd, VarianteColor, Proveedor, Promocion,
)
from app.modules.seguridad_accesos.services.bitacora import record

INPUTS = {'tallas': schema.TallaDatos, 'colores': schema.ColorDatos, 'colecciones': schema.ColeccionDatos,
          'temporadas': schema.TemporadaDatos, 'categorias': schema.CategoriaDatos, 'marcas': schema.MarcaDatos,
          'proveedores': schema.ProveedorDatos}


def parse(group, value):
    repo.group_info(group)
    try:
        return INPUTS[group].model_validate(value)
    except ValidationError:
        raise DomainError(422, 'datos_invalidos', 'Completa los campos obligatorios y revisa sus valores') from None


def save_group(db, group, value, id, actor, peer):
    data = parse(group, value)
    permission = 'CU19' if group == 'proveedores' else 'CU18'
    with transaction(db):
        model, pk, _ = repo.group_info(group)
        row = repo.get(db, model, id, lock=True) if id is not None else model()
        actor_scope(db, actor, permission)
        if id is None and group in {'tallas', 'colores'}:
            setattr(row, pk, repo.allocate_smallint(db, model, pk))
        values = data.model_dump(exclude={'idTemps'})
        for key, value in values.items():
            setattr(row, {'fechaIni': 'fechaini', 'fechaFin': 'fechafin'}.get(key, key), str(value) if key == 'correo' else value)
        if group == 'colecciones':
            for temp in sorted(data.idTemps):
                repo.get(db, Temporada, temp, reference=True)
        db.add(row); db.flush()
        if group == 'colecciones':
            db.execute(delete(TempColeccion).where(TempColeccion.idcol == row.idcol))
            db.add_all([TempColeccion(idcol=row.idcol, idtemp=t) for t in data.idTemps]); db.flush()
        record(db, 'proveedor_guardado' if group == 'proveedores' else 'catalogo_guardado', actor, peer, True)
        result = repo.serialize(db, group, row)
    return result


def remove_group(db, group, id, actor, peer):
    permission = 'CU19' if group == 'proveedores' else 'CU18'
    with transaction(db):
        model, _, _ = repo.group_info(group)
        row = repo.get(db, model, id, lock=True)
        actor_scope(db, actor, permission)
        allowed = ('tempcoleccion',) if group in {'colecciones', 'temporadas'} else ()
        repo.prevent_cascade(db, row, allowed)
        if group == 'colecciones':
            db.execute(delete(TempColeccion).where(TempColeccion.idcol == id))
        elif group == 'temporadas':
            db.execute(delete(TempColeccion).where(TempColeccion.idtemp == id))
        db.delete(row); db.flush()
        record(db, 'proveedor_eliminado' if group == 'proveedores' else 'catalogo_eliminado', actor, peer, True)


def save_product(db, data, id, actor, peer):
    with transaction(db):
        row = repo.get(db, Producto, id, lock=True) if id else Producto(idprod='Prod-' + token_hex(10))
        actor_scope(db, actor, 'CU18')
        for model, reference in [(Categoria, data.idCat), (Marca, data.idMarca), (Coleccion, data.idCol), (Proveedor, data.idProv)]:
            repo.get(db, model, reference, reference=True)
        if data.idPromo is not None:
            repo.get(db, Promocion, data.idPromo, reference=True)
        existing = {v.idvariante: v for v in db.scalars(select(VarianteProd).where(VarianteProd.idprod == row.idprod)
                    .order_by(VarianteProd.idvariante).with_for_update()).all()}
        desired = {v.idVariante for v in data.variantes if v.idVariante is not None}
        if not desired.issubset(existing):
            raise DomainError(422, 'variante_invalida', 'Una variante no pertenece a esta prenda')
        skus = [v.sku for v in data.variantes]
        if db.scalar(select(VarianteProd.idvariante).where(VarianteProd.sku.in_(skus),
                     VarianteProd.idvariante.not_in(list(existing))).limit(1)):
            raise DomainError(409, 'sku_duplicado', 'El SKU ya está registrado en otra variante')
        # No se permite intercambiar SKU entre variantes: evita violar UNIQUE durante el flush.
        for v in data.variantes:
            if any(e.sku == v.sku and e.idvariante != v.idVariante for e in existing.values()):
                raise DomainError(409, 'sku_duplicado', 'El SKU pertenece a otra variante de esta prenda')
        for key, value in data.model_dump(exclude={'variantes'}).items():
            setattr(row, {'idCat': 'idcat', 'idMarca': 'idmarca', 'idCol': 'idcol', 'idProv': 'idprov', 'idPromo': 'idpromo'}.get(key, key), value)
        db.add(row); db.flush()
        for removed in sorted(set(existing) - desired):
            variant = existing[removed]
            repo.prevent_cascade(db, variant, ('variantecolors',))
            db.execute(delete(VarianteColor).where(VarianteColor.idvar == removed)); db.delete(variant)
        for v in data.variantes:
            repo.get(db, Talla, v.idTalla, reference=True)
            for color in sorted(v.idColores):
                repo.get(db, Color, color, reference=True)
            variant = existing.get(v.idVariante) if v.idVariante else VarianteProd(idvariante=token_hex(7), idprod=row.idprod)
            variant.sku, variant.precio, variant.estado, variant.img, variant.idtalla = v.sku, v.precio, v.estado, v.img, v.idTalla
            db.add(variant); db.flush()
            db.execute(delete(VarianteColor).where(VarianteColor.idvar == variant.idvariante))
            db.add_all([VarianteColor(idvar=variant.idvariante, idcolor=c) for c in v.idColores]); db.flush()
        record(db, 'catalogo_guardado', actor, peer, True)
        result = repo.product_detail(db, row)
    return result


def remove_product(db, id, actor, peer):
    with transaction(db):
        row = repo.get(db, Producto, id, lock=True)
        actor_scope(db, actor, 'CU18')
        variants = db.scalars(select(VarianteProd).where(VarianteProd.idprod == id)
                             .order_by(VarianteProd.idvariante).with_for_update()).all()
        for variant in variants:
            repo.prevent_cascade(db, variant, ('variantecolors',))
            db.execute(delete(VarianteColor).where(VarianteColor.idvar == variant.idvariante)); db.delete(variant)
        db.flush(); repo.prevent_cascade(db, row); db.delete(row); db.flush()
        record(db, 'catalogo_eliminado', actor, peer, True)
