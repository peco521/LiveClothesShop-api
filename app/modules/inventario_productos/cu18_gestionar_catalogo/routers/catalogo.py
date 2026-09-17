from typing import Literal

from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile
from app.integrations.cloudinary_images import MAX_IMAGE_BYTES, upload_image

from app.core.database import get_db
from app.modules.inventario_productos.shared.access import access
from app.modules.inventario_productos.cu18_gestionar_catalogo.schemas.catalogo import ProductoDatos
from app.modules.inventario_productos.cu18_gestionar_catalogo.repositories import catalogo as repo
from app.modules.inventario_productos.cu18_gestionar_catalogo.services import catalogo as service
from app.modules.cliente_experiencia_compra.shared.models.catalogo import Producto

identity = access('CU18')
router = APIRouter(prefix='/api/admin/catalogo', tags=['CU18 Gestionar Catálogo'], dependencies=[Depends(identity)])
Group = Literal['tallas', 'colores', 'colecciones', 'temporadas', 'categorias', 'marcas']


def peer(request):
    return request.client.host if request.client else None


@router.post('/imagenes', status_code=201)
def image_upload(request: Request, file: UploadFile = File(...)):
    try:
        return upload_image(request.app.state.settings, file.file.read(MAX_IMAGE_BYTES + 1), file.content_type)
    finally:
        file.file.close()


@router.get('/referencias')
def references(db=Depends(get_db)):
    return repo.references(db)


@router.get('/productos')
def products(q: str = Query('', max_length=100), estado: Literal['activo', 'inactivo'] | None = None,
             offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100), db=Depends(get_db)):
    return repo.list_products(db, q.strip(), estado, offset, limit)


@router.get('/productos/{id}')
def detail(id: str, db=Depends(get_db)):
    return repo.product_detail(db, repo.get(db, Producto, id))


@router.post('/productos', status_code=201)
def create(data: ProductoDatos, request: Request, db=Depends(get_db), actor=Depends(identity)):
    return service.save_product(db, data, None, actor[0], peer(request))


@router.put('/productos/{id}')
def edit(id: str, data: ProductoDatos, request: Request, db=Depends(get_db), actor=Depends(identity)):
    return service.save_product(db, data, id, actor[0], peer(request))


@router.delete('/productos/{id}', status_code=204)
def remove(id: str, request: Request, db=Depends(get_db), actor=Depends(identity)):
    service.remove_product(db, id, actor[0], peer(request))
    return Response(status_code=204)


@router.get('/{group}')
def listing(group: Group, q: str = Query('', max_length=100), offset: int = Query(0, ge=0),
            limit: int = Query(20, ge=1, le=100), db=Depends(get_db)):
    return repo.list_group(db, group, q.strip(), offset, limit)


@router.get('/{group}/{id}')
def group_detail(group: Group, id: int, db=Depends(get_db)):
    model, _, _ = repo.group_info(group)
    return repo.serialize(db, group, repo.get(db, model, id))


@router.post('/{group}', status_code=201)
def group_create(group: Group, data: dict, request: Request, db=Depends(get_db), actor=Depends(identity)):
    return service.save_group(db, group, data, None, actor[0], peer(request))


@router.put('/{group}/{id}')
def group_edit(group: Group, id: int, data: dict, request: Request, db=Depends(get_db), actor=Depends(identity)):
    return service.save_group(db, group, data, id, actor[0], peer(request))


@router.delete('/{group}/{id}', status_code=204)
def group_remove(group: Group, id: int, request: Request, db=Depends(get_db), actor=Depends(identity)):
    service.remove_group(db, group, id, actor[0], peer(request))
    return Response(status_code=204)
