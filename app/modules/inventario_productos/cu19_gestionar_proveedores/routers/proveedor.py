from fastapi import APIRouter, Depends, Query, Request, Response

from app.core.database import get_db
from app.modules.inventario_productos.shared.access import access
from app.modules.inventario_productos.cu18_gestionar_catalogo.schemas.catalogo import ProveedorDatos
from app.modules.inventario_productos.cu18_gestionar_catalogo.repositories import catalogo as repo
from app.modules.inventario_productos.cu18_gestionar_catalogo.services import catalogo as service
from app.modules.cliente_experiencia_compra.shared.models.catalogo import Proveedor

identity = access('CU19')
router = APIRouter(prefix='/api/admin/proveedores', tags=['CU19 Gestionar Proveedores'], dependencies=[Depends(identity)])


@router.get('')
def listing(q: str = Query('', max_length=100), offset: int = Query(0, ge=0),
            limit: int = Query(20, ge=1, le=100), db=Depends(get_db)):
    return repo.list_group(db, 'proveedores', q.strip(), offset, limit)


@router.get('/{id}')
def detail(id: int, db=Depends(get_db)):
    return repo.serialize(db, 'proveedores', repo.get(db, Proveedor, id))


@router.post('', status_code=201)
def create(data: ProveedorDatos, request: Request, db=Depends(get_db), actor=Depends(identity)):
    return service.save_group(db, 'proveedores', data.model_dump(), None, actor[0], request.client.host if request.client else None)


@router.put('/{id}')
def edit(id: int, data: ProveedorDatos, request: Request, db=Depends(get_db), actor=Depends(identity)):
    return service.save_group(db, 'proveedores', data.model_dump(), id, actor[0], request.client.host if request.client else None)


@router.delete('/{id}', status_code=204)
def remove(id: int, request: Request, db=Depends(get_db), actor=Depends(identity)):
    service.remove_group(db, 'proveedores', id, actor[0], request.client.host if request.client else None)
    return Response(status_code=204)
