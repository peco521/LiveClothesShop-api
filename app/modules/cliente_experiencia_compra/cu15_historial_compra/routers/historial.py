from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.modules.cliente_experiencia_compra.cu15_historial_compra.schemas.historial import (
    CompraDetalle,
    HistorialCompras,
)
from app.modules.cliente_experiencia_compra.cu15_historial_compra.services import historial as service
from app.modules.cliente_experiencia_compra.shared.dependencies import require_cliente

router = APIRouter(
    prefix="/api/cliente/historial-compras",
    tags=["CU15 Historial de compras"],
    dependencies=[Depends(require_cliente)],
)


@router.get("", response_model=HistorialCompras, response_model_exclude_none=True)
def list_history(
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    identity=Depends(require_cliente),
):
    return service.listar(db, identity.usuario.idUsuario, offset, limit)


@router.get("/{nroVenta}", response_model=CompraDetalle, response_model_exclude_none=True)
def purchase_detail(
    nroVenta: Annotated[int, Path(ge=1, le=2147483647)],
    db: Session = Depends(get_db),
    identity=Depends(require_cliente),
):
    return service.detalle(db, nroVenta, identity.usuario.idUsuario)
