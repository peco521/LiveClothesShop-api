from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import require_permission
from app.modules.seguridad_accesos.cu07_clientes.schemas.cliente import (
    ClienteCrear, ClienteDetalle, ClienteEditar, ClientesListado, EstadoCuenta,
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


@router.post("", response_model=ClienteDetalle, status_code=201)
def create_client(data: ClienteCrear, request: Request, db: Session = Depends(get_db),
                  identity=Depends(cu07_identity)):
    # CU07 alta administrativa: crea el cliente manteniendo la sesión del administrador.
    return cliente.create(db, data, request.app.state.settings, request.app.state.passwords,
                          identity.usuario.idUsuario, peer(request))


@router.get("/{idUsuario}", response_model=ClienteDetalle)
def detail(idUsuario: str, request: Request, db: Session = Depends(get_db)):
    return cliente.get_detail(db, idUsuario, request.app.state.settings)


@router.patch("/{idUsuario}/estado-cuenta", response_model=ClienteDetalle)
def set_client_state(idUsuario: str, data: EstadoCuenta, request: Request, db: Session = Depends(get_db),
                     identity=Depends(cu07_identity)):
    # CU07 baja lógica: inactivo conserva ventas, reservas e historial.
    return cliente.set_state(db, idUsuario, data, request.app.state.settings,
                             identity.usuario.idUsuario, peer(request))


@router.patch("/{idUsuario}", response_model=ClienteDetalle)
def edit(idUsuario: str, data: ClienteEditar, request: Request, db: Session = Depends(get_db),
         identity=Depends(cu07_identity)):
    return cliente.edit(db, idUsuario, data, request.app.state.settings, identity.usuario.idUsuario, peer(request))
