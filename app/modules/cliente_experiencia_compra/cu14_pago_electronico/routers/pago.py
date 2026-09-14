from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.modules.cliente_experiencia_compra.cu14_pago_electronico.schemas.pago import (
    PagoCrear,
    PagoDetalle,
    PagoReprocesar,
)
from app.modules.cliente_experiencia_compra.cu14_pago_electronico.services import pago as service
from app.modules.cliente_experiencia_compra.shared.dependencies import require_cliente

router = APIRouter(prefix="/api/cliente/pagos", tags=["Pago electrónico"],
                   dependencies=[Depends(require_cliente)])


def peer(request):
    return request.client.host if request.client else None


def pasarela(request):
    return request.app.state.pasarela


def environment(request):
    return request.app.state.settings.environment


@router.post("", response_model=PagoDetalle, status_code=201)
def pay(data: PagoCrear, request: Request, response: Response,
        db: Session = Depends(get_db), identity=Depends(require_cliente)):
    vista, creado = service.iniciar(db, data, identity.usuario.idUsuario, peer(request),
                                    pasarela(request), environment(request))
    # Idempotencia: pago existente reutilizado se devuelve con 200.
    if not creado:
        response.status_code = 200
    return vista


@router.get("/{idPago}", response_model=PagoDetalle)
def detail(idPago: int, request: Request, db: Session = Depends(get_db),
           identity=Depends(require_cliente)):
    return service.consultar(db, idPago, identity.usuario.idUsuario)


@router.post("/{idPago}/procesar", response_model=PagoDetalle)
def reprocess(idPago: int, data: PagoReprocesar, request: Request,
              db: Session = Depends(get_db), identity=Depends(require_cliente)):
    vista, _ = service.reprocesar(db, idPago, data, identity.usuario.idUsuario,
                                  peer(request), pasarela(request), environment(request))
    return vista
