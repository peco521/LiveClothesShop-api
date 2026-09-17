from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import require_permission
from app.modules.seguridad_accesos.cu07_clientes.schemas.cliente import (
    ClienteDetalle, ClienteEditar, ClientesListado,
)
from app.modules.seguridad_accesos.cu07_clientes.services import cliente

cu07_identity = require_permission("CU07")
router = APIRouter(prefix="/api/admin/clientes", tags=["CU07 Clientes"],
                   dependencies=[Depends(cu07_identity)])


def peer(request):
    return request.client.host if request.client else None


@router.get("", response_model=ClientesListado)
def list_clients(request: Request, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100),
                 q: str = Query("", max_length=100),
                 db: Session = Depends(get_db)):
    return cliente.list_clients(db, request.app.state.settings, offset=offset, limit=limit, q=q.strip())


@router.get("/{idUsuario}", response_model=ClienteDetalle)
def detail(idUsuario: str, request: Request, db: Session = Depends(get_db)):
    return cliente.get_detail(db, idUsuario, request.app.state.settings)


@router.patch("/{idUsuario}", response_model=ClienteDetalle)
def edit(idUsuario: str, data: ClienteEditar, request: Request, db: Session = Depends(get_db),
         identity=Depends(cu07_identity)):
    return cliente.edit(db, idUsuario, data, request.app.state.settings, identity.usuario.idUsuario, peer(request))
