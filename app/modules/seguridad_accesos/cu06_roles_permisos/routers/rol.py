from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import require_permission
from app.modules.seguridad_accesos.cu06_roles_permisos.schemas.rol import (
    EstadoCuenta, FuncionDetalle, Identifier, PermisosDetalle, PermisosReemplazar, RolCrear, RolDetalle, RolEditar, RolesListado,
)
from app.modules.seguridad_accesos.cu06_roles_permisos.services import rol

cu06_identity = require_permission("CU06")
router = APIRouter(prefix="/api/admin", tags=["CU06 Roles y Permisos"], dependencies=[Depends(cu06_identity)])


def peer(request):
    return request.client.host if request.client else None


@router.get("/roles", response_model=RolesListado)
def list_roles(request: Request, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100),
               db: Session = Depends(get_db)):
    return rol.list_roles(db, request.app.state.settings, offset, limit)


@router.post("/roles", response_model=RolDetalle, status_code=201)
def create(data: RolCrear, request: Request, db: Session = Depends(get_db), identity=Depends(cu06_identity)):
    return rol.create(db, data, request.app.state.settings, identity.usuario.idUsuario, peer(request))


@router.get("/funciones", response_model=list[FuncionDetalle])
def functions(db: Session = Depends(get_db)):
    return rol.functions(db)


@router.get("/roles/{nro}", response_model=RolDetalle)
def detail(nro: Identifier, request: Request, db: Session = Depends(get_db)):
    return rol.detail(rol.get(db, nro), request.app.state.settings)


@router.patch("/roles/{nro}/estado-cuenta", response_model=RolDetalle)
def set_role_state(nro: Identifier, data: EstadoCuenta, request: Request, db: Session = Depends(get_db),
                   identity=Depends(cu06_identity)):
    # CU06: baja lógica del rol. El rol público de clientes está protegido.
    return rol.set_state(db, nro, data, request.app.state.settings, identity.usuario.idUsuario, peer(request))


@router.patch("/roles/{nro}", response_model=RolDetalle)
def edit(nro: Identifier, data: RolEditar, request: Request, db: Session = Depends(get_db), identity=Depends(cu06_identity)):
    return rol.edit(db, nro, data, request.app.state.settings, identity.usuario.idUsuario, peer(request))


@router.get("/roles/{nro}/permisos", response_model=PermisosDetalle)
def permissions(nro: Identifier, request: Request, db: Session = Depends(get_db)):
    return rol.permissions(db, nro, request.app.state.settings)


@router.put("/roles/{nro}/permisos", response_model=PermisosDetalle)
def replace_permissions(nro: Identifier, data: PermisosReemplazar, request: Request,
                        db: Session = Depends(get_db), identity=Depends(cu06_identity)):
    return rol.replace_permissions(db, nro, data, request.app.state.settings, identity.usuario.idUsuario, peer(request))
