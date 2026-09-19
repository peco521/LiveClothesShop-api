from decimal import Decimal

from fastapi import APIRouter, Depends, Query
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

router = APIRouter(prefix="/api/catalogo", tags=["Catálogo"],
                   dependencies=[Depends(require_cliente)])


@router.get("/productos", response_model=ProductosListado)
def list_products(offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100),
                  q: str = Query("", max_length=100),
                  idCat: int | None = Query(None, ge=1),
                  idMarca: int | None = Query(None, ge=1),
                  idCol: int | None = Query(None, ge=1),
                  idTemp: int | None = Query(None, ge=1),
                  idTalla: int | None = Query(None),
                  idColor: int | None = Query(None),
                  minPrecio: Decimal | None = Query(None, ge=0),
                  maxPrecio: Decimal | None = Query(None, ge=0),
                  soloDisponibles: bool = Query(False),
                  sort: str = Query("nombre_asc", pattern="^(nombre_asc|nombre_desc|precio_asc|precio_desc)$"),
                  db: Session = Depends(get_db)):
    filters = CatalogoFiltros(offset=offset, limit=limit, q=q.strip(),
                              idCat=idCat, idMarca=idMarca, idCol=idCol, idTemp=idTemp,
                              idTalla=idTalla, idColor=idColor,
                              minPrecio=minPrecio, maxPrecio=maxPrecio,
                              soloDisponibles=soloDisponibles, sort=sort)  # type: ignore[arg-type]
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
