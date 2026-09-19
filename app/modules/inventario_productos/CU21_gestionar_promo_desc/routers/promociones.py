from fastapi import APIRouter, Depends, Query, Request, Response
from app.core.database import get_db
from app.modules.inventario_productos.shared.operaciones_access import access
from app.modules.inventario_productos.shared.operaciones_referencias import add_references, peer
from app.modules.inventario_productos.CU21_gestionar_promo_desc.services import promociones
from app.modules.inventario_productos.CU21_gestionar_promo_desc.schemas.promociones import PromotionInput

router = APIRouter(prefix='/api/admin/promociones', tags=['CU21'])
a21 = access('CU21')
add_references(router, a21)


@router.get('')
def promotion_list(q: str = Query('', max_length=100), offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100), db=Depends(get_db), actor=Depends(a21)):
    return promociones.listing(db, q, offset, limit)


@router.post('', status_code=201)
def promotion_create(data: PromotionInput, request: Request, db=Depends(get_db), actor=Depends(a21)):
    return promociones.save(db, data, actor[0], peer(request))


@router.put('/{nro}')
def promotion_edit(nro: int, data: PromotionInput, request: Request, db=Depends(get_db), actor=Depends(a21)):
    return promociones.save(db, data, actor[0], peer(request), nro)


@router.post('/{nro}/desactivar')
def promotion_deactivate(nro: int, request: Request, db=Depends(get_db), actor=Depends(a21)):
    return promociones.deactivate(db, nro, actor[0], peer(request))
