from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.modules.cliente_experiencia_compra.cu11_gestionar_reserva.schemas.reserva import (
    EstadoReserva,
    ReservaCrear,
    ReservaDetalle,
    ReservasFiltros,
    ReservasListado,
)
from app.modules.cliente_experiencia_compra.cu11_gestionar_reserva.services import reserva as service
from app.modules.cliente_experiencia_compra.shared.dependencies import require_cliente

router = APIRouter(prefix="/api/cliente/reservas", tags=["Reservas"],
                   dependencies=[Depends(require_cliente)])


def peer(request):
    return request.client.host if request.client else None


@router.post("", response_model=ReservaDetalle, status_code=201)
def create(data: ReservaCrear, request: Request, db: Session = Depends(get_db),
           identity=Depends(require_cliente)):
    return service.crear(db, data, identity.usuario.idUsuario, peer(request))


@router.get("", response_model=ReservasListado)
def list_reservas(request: Request, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100),
                  estado: EstadoReserva | None = Query(None),
                  db: Session = Depends(get_db), identity=Depends(require_cliente)):
    filters = ReservasFiltros(offset=offset, limit=limit, estado=estado)
    return service.listar(db, filters, identity.usuario.idUsuario, peer(request))


@router.get("/{nroReserva}", response_model=ReservaDetalle)
def detail(nroReserva: int, request: Request, db: Session = Depends(get_db),
           identity=Depends(require_cliente)):
    return service.detalle(db, nroReserva, identity.usuario.idUsuario, peer(request))


@router.patch("/{nroReserva}/cancelar", response_model=ReservaDetalle)
def cancel(nroReserva: int, request: Request, db: Session = Depends(get_db),
           identity=Depends(require_cliente)):
    return service.cancelar(db, nroReserva, identity.usuario.idUsuario, peer(request))
