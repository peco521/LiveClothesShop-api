from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.modules.cliente_experiencia_compra.cu12_carrito.schemas.carrito import (
    CarritoDetalle,
    ItemAgregar,
    ItemCantidad,
)
from app.modules.cliente_experiencia_compra.cu12_carrito.services import carrito as service
from app.modules.cliente_experiencia_compra.shared.dependencies import require_cliente

router = APIRouter(prefix="/api/cliente/carrito", tags=["Carrito"],
                   dependencies=[Depends(require_cliente)])


def peer(request):
    return request.client.host if request.client else None


@router.get("", response_model=CarritoDetalle)
def get_cart(db: Session = Depends(get_db), identity=Depends(require_cliente)):
    # Read-only: jamás crea carrito.
    return service.obtener(db, identity.usuario.idUsuario)


@router.post("/items", response_model=CarritoDetalle, status_code=201)
def add_item(data: ItemAgregar, request: Request, db: Session = Depends(get_db),
             identity=Depends(require_cliente)):
    return service.agregar(db, data, identity.usuario.idUsuario, peer(request))


@router.patch("/items/{idDetalleCarro}", response_model=CarritoDetalle)
def set_quantity(idDetalleCarro: int, data: ItemCantidad, request: Request,
                 db: Session = Depends(get_db), identity=Depends(require_cliente)):
    return service.modificar(db, idDetalleCarro, data, identity.usuario.idUsuario, peer(request))


@router.delete("/items/{idDetalleCarro}", response_model=CarritoDetalle)
def remove_item(idDetalleCarro: int, request: Request,
                db: Session = Depends(get_db), identity=Depends(require_cliente)):
    return service.eliminar(db, idDetalleCarro, identity.usuario.idUsuario, peer(request))
