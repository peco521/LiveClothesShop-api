from fastapi import APIRouter, Depends, Query, Request

from app.core.database import get_db
from app.modules.inventario_productos.shared.access import access
from app.modules.inventario_productos.cu18_gestionar_catalogo.repositories.catalogo import get
from app.modules.inventario_productos.cu20_gestionar_inventario.schemas.inventario import MovimientoDatos
from app.modules.inventario_productos.cu20_gestionar_inventario.repositories import inventario as repo
from app.modules.inventario_productos.cu20_gestionar_inventario.services import inventario as service
from app.modules.cliente_experiencia_compra.shared.models.catalogo import Inventario

identity = access('CU20', True)
router = APIRouter(prefix='/api/admin/inventario', tags=['CU20 Gestionar Inventario'], dependencies=[Depends(identity)])


@router.get('/referencias')
def references(db=Depends(get_db), actor=Depends(identity)):
    return repo.references(db, actor[1])


@router.get('')
def listing(q: str = Query('', max_length=100), nroSuc: int | None = None, idCat: int | None = None,
            idTalla: int | None = None, idColor: int | None = None, offset: int = Query(0, ge=0),
            limit: int = Query(20, ge=1, le=100), db=Depends(get_db), actor=Depends(identity)):
    if nroSuc is not None:
        service.enforce_branch(actor[1], nroSuc)
    return repo.listing(db, actor[1], q=q.strip(), nroSuc=nroSuc, idCat=idCat, idTalla=idTalla,
                        idColor=idColor, offset=offset, limit=limit)


@router.post('/movimientos', status_code=201)
def movement(data: MovimientoDatos, request: Request, db=Depends(get_db), actor=Depends(identity)):
    return service.register(db, data, actor[0], request.client.host if request.client else None)


@router.get('/{id}/movimientos')
def history(id: int, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100),
            db=Depends(get_db), actor=Depends(identity)):
    row = get(db, Inventario, id)
    service.enforce_branch(actor[1], row.nrosuc)
    return repo.history(db, id, offset, limit)
