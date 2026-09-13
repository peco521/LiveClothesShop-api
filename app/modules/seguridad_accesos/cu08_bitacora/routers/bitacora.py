from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import require_permission
from app.core.errors import DomainError
from app.modules.seguridad_accesos.cu08_bitacora.schemas.bitacora import (
    BitacoraDetalle, BitacoraFiltros, BitacoraListado,
)
from app.modules.seguridad_accesos.cu08_bitacora.services import bitacora

router = APIRouter(prefix="/api/admin/bitacora", tags=["CU08 Bitácora"],
                   dependencies=[Depends(require_permission("CU08"))])


@router.get("", response_model=BitacoraListado)
def list_events(filters: Annotated[BitacoraFiltros, Query()], db: Session = Depends(get_db)):
    return bitacora.list_events(db, filters)


@router.get("/{id}", response_model=BitacoraDetalle)
def detail(id: Annotated[str, Path(pattern=r"^[1-9][0-9]{0,18}$")], db: Session = Depends(get_db)):
    event_id = int(id)
    if event_id > 9223372036854775807:
        raise DomainError(422, "datos_invalidos", "Los datos enviados no son válidos")
    return bitacora.detail(db, event_id)
