from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.modules.cliente_experiencia_compra.cu13_compra_digital.schemas.compra import (
    CompraCrear,
    VentaDetalle,
)
from app.modules.cliente_experiencia_compra.cu13_compra_digital.services import compra as service
from app.modules.cliente_experiencia_compra.shared.dependencies import require_cliente

router = APIRouter(prefix="/api/cliente/compras", tags=["Compra digital"],
                   dependencies=[Depends(require_cliente)])


def peer(request):
    return request.client.host if request.client else None


@router.post("/desde-carrito", response_model=VentaDetalle, status_code=201)
def checkout(data: CompraCrear, request: Request, response: Response,
             db: Session = Depends(get_db), identity=Depends(require_cliente)):
    vista, creada = service.checkout(db, data, identity.usuario.idUsuario, peer(request))
    # Idempotencia: el reintento devuelve la venta pendiente existente con 200.
    if not creada:
        response.status_code = 200
    return vista


@router.get("/{nroVenta}", response_model=VentaDetalle)
def detail(nroVenta: int, db: Session = Depends(get_db),
           identity=Depends(require_cliente)):
    return service.detalle(db, nroVenta, identity.usuario.idUsuario)
