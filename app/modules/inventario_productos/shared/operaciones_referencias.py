from fastapi import Depends, Query
from sqlalchemy import select
from app.core.database import get_db
from app.modules.seguridad_accesos.shared.models import Sucursal, Ciudad
from app.modules.cliente_experiencia_compra.shared.models.catalogo import Categoria, Temporada
from app.modules.cliente_experiencia_compra.cu10_consultar_prendas.services import catalogo
from app.modules.cliente_experiencia_compra.cu10_consultar_prendas.schemas.catalogo import CatalogoFiltros


def peer(request): return request.client.host if request.client else None


def add_references(target, gate):
    @target.get('/referencias')
    def references(db=Depends(get_db), actor=Depends(gate)):
        stores = select(Sucursal, Ciudad).join(Ciudad, Ciudad.id == Sucursal.idciud).where(Sucursal.estado == 'activo')
        if actor[1] is not None:
            stores = stores.where(Sucursal.nro == actor[1])
        return dict(sucursalAsignada=actor[1], sucursales=[dict(nro=s.nro, nombre=s.nombre, ciudad=c.nombre) for s, c in db.execute(stores)], categorias=[dict(id=c.idcat, nombre=c.descripcion) for c in db.scalars(select(Categoria).order_by(Categoria.descripcion))], temporadas=[dict(id=t.idtemp, nombre=t.nombre) for t in db.scalars(select(Temporada).order_by(Temporada.nombre))])
    @target.get('/productos')
    def products(q: str = Query('', max_length=100), offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100), db=Depends(get_db), actor=Depends(gate)):
        return catalogo.list_products(db, CatalogoFiltros(q=q, offset=offset, limit=limit))
    @target.get('/productos/{idProd}')
    def product(idProd: str, db=Depends(get_db), actor=Depends(gate)):
        result = catalogo.product_detail(db, idProd)
        if actor[1] is not None:
            result.disponibilidad = [row for row in result.disponibilidad if row.nroSuc == actor[1]]
        return result
