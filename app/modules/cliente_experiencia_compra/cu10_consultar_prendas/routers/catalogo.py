from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.errors import DomainError
from app.modules.cliente_experiencia_compra.cu10_consultar_prendas.schemas.catalogo import (    CatalogoFiltros,
    FacetasListado,
    ProductoDetalle,
    ProductosListado,
)
from app.modules.cliente_experiencia_compra.cu10_consultar_prendas.services import catalogo
from app.modules.cliente_experiencia_compra.shared.dependencies import require_cliente

def catalog_session(request: Request, identity=Depends(require_cliente)):
    # CU10 is not a public storefront. The login resolves the company and the
    # authenticated session fixes it; selectors here must never override it.
    if (any(key in request.query_params for key in ("empresa", "idEmp", "idemp", "tenant"))
            or any(key in request.headers for key in ("x-empresa", "x-idemp", "x-tenant-id"))):
        raise DomainError(422, "datos_invalidos", "Los datos enviados no son válidos")
    return identity


router = APIRouter(prefix="/api/catalogo", tags=["Catálogo"],
                   dependencies=[Depends(catalog_session)])


@router.get("/productos", response_model=ProductosListado)
def list_products(filters: Annotated[CatalogoFiltros, Query()],
                  db: Session = Depends(get_db)):
    return catalogo.list_products(db, filters)


@router.get("/productos/{idProd}", response_model=ProductoDetalle)
def product_detail(idProd: str, db: Session = Depends(get_db)):
    return catalogo.product_detail(db, idProd)


@router.get("/variantes/{idVar}", response_model=ProductoDetalle)
def variant_product_detail(idVar: str, db: Session = Depends(get_db)):
    return catalogo.variant_product_detail(db, idVar)


_FACETAS = ("categorias", "marcas", "colecciones", "temporadas", "tallas", "colores")


@router.get("/{grupo}", response_model=FacetasListado)
def facet_list(grupo: str, idCat: int | None = Query(None, ge=1), db: Session = Depends(get_db)):
    if grupo not in _FACETAS:
        raise DomainError(404, "faceta_no_encontrada", "Filtro no encontrado")
    return catalogo.facets(db, grupo, idCat=idCat)
