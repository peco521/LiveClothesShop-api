from fastapi import APIRouter, Depends, Query, Request, Response
from app.core.database import get_db
from app.modules.inventario_productos.shared.operaciones_access import access
from app.modules.inventario_productos.shared.operaciones_referencias import add_references, peer
from app.modules.inventario_productos.cu22_gestionar_reservas_sucursal.services import reservas
from app.modules.inventario_productos.cu22_gestionar_reservas_sucursal.schemas.reservas import ReservationAction
from typing import Literal


router = APIRouter(prefix='/api/admin/reservas-sucursal', tags=['CU22'])
a22 = access('CU22')
add_references(router, a22)


@router.get('')
def reservation_list(estado: Literal['pendiente', 'confirmada', 'atendida', 'cancelada', 'vencida'] | None = None, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100), db=Depends(get_db), actor=Depends(a22)):
    return reservas.listing(db, actor[1], estado, offset, limit)


@router.get('/{nro}')
def reservation_detail(nro: int, db=Depends(get_db), actor=Depends(a22)):
    reservas.expire(db, actor[1])
    return reservas.detalle(db, reservas.get(db, nro, actor[1]))


@router.post('/{nro}/accion')
def reservation_update(nro: int, data: ReservationAction, request: Request, db=Depends(get_db), actor=Depends(a22)):
    return reservas.update(db, nro, data, actor[0], actor[1], peer(request))
