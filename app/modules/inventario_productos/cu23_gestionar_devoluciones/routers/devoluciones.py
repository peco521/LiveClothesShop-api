from fastapi import APIRouter, Depends, Query, Request, Response
from app.core.database import get_db
from app.modules.inventario_productos.shared.operaciones_access import access
from app.modules.inventario_productos.shared.operaciones_referencias import add_references, peer
from app.modules.inventario_productos.cu23_gestionar_devoluciones.services import devoluciones
from app.modules.inventario_productos.cu23_gestionar_devoluciones.schemas.devoluciones import ReturnInput, ReturnDecision, PolicyInput
from sqlalchemy import select
from app.core.errors import DomainError
from app.modules.inventario_productos.cu23_gestionar_devoluciones.models.devoluciones import PoliticaDevolucion

router = APIRouter(prefix='/api/admin/devoluciones', tags=['CU23'])
a23 = access('CU23')
add_references(router, a23)


@router.get('/politicas')
def policy_list(db=Depends(get_db), actor=Depends(a23)):
    return [devoluciones.policy_view(row, db) for row in db.scalars(select(PoliticaDevolucion).order_by(PoliticaDevolucion.id))]


@router.put('/politicas')
def policy_save(data: PolicyInput, request: Request, db=Depends(get_db), actor=Depends(a23)):
    if actor[1] is not None:
        raise DomainError(403, 'acceso_denegado', 'Solo el administrador puede modificar las políticas')
    return devoluciones.policy_save(db, data, actor[0], peer(request))


@router.get('/ventas/{nro}')
def return_sale(nro: int, db=Depends(get_db), actor=Depends(a23)):
    from app.modules.cliente_experiencia_compra.cu13_compra_digital.services.compra import _vista
    row = devoluciones.sale(db, nro, actor[1])
    result = _vista(db, row).model_dump(); result['idCliente'] = row.idusuariocl
    return result


@router.get('')
def return_list(offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100), db=Depends(get_db), actor=Depends(a23)):
    return devoluciones.listing(db, actor[1], offset, limit)


@router.post('', status_code=201)
def return_create(data: ReturnInput, request: Request, db=Depends(get_db), actor=Depends(a23)):
    return devoluciones.create(db, data, actor[0], actor[1], peer(request))


@router.post('/{nro}/decision')
def return_decide(nro: int, data: ReturnDecision, request: Request, db=Depends(get_db), actor=Depends(a23)):
    return devoluciones.decide(db, nro, data, actor[0], actor[1], peer(request))


@router.post('/{nro}/reembolsar')
def return_refund(nro: int, request: Request, db=Depends(get_db), actor=Depends(a23)):
    return devoluciones.refund(db, nro, actor[0], actor[1], peer(request), request.app.state.stripe)
