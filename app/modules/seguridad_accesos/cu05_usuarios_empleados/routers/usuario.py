from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import require_permission
from app.modules.seguridad_accesos.schemas.auth import RolResponse
from app.modules.seguridad_accesos.cu05_usuarios_empleados.schemas.usuario import (
    CiudadOpcion, EmpleadoCrear, EmpleadoEditar, SucursalOpcion, UsuarioDetalle, UsuarioEstado, UsuariosListado,
)
from app.modules.seguridad_accesos.cu05_usuarios_empleados.services import usuario

cu05_identity = require_permission("CU05")
router = APIRouter(prefix="/api/admin", tags=["CU05 Usuarios y Empleados"],
                   dependencies=[Depends(cu05_identity)])


def peer(request):
    return request.client.host if request.client else None


@router.get("/usuarios", response_model=UsuariosListado)
def list_users(offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100),
               q: str = Query("", max_length=100), tipo: Literal["A", "E"] | None = None,
               activo: bool | None = None, db: Session = Depends(get_db)):
    return usuario.list_users(db, offset=offset, limit=limit, q=q.strip(), tipo=tipo, activo=activo)


@router.get("/usuarios/roles", response_model=list[RolResponse])
def roles(request: Request, db: Session = Depends(get_db)):
    return usuario.roles(db, request.app.state.settings)


@router.get("/empleados/ciudades", response_model=list[CiudadOpcion])
def cities(db: Session = Depends(get_db)):
    return usuario.cities(db)


@router.get("/empleados/sucursales", response_model=list[SucursalOpcion])
def branches(idCiud: int | None = Query(None, ge=-32768, le=32767), db: Session = Depends(get_db)):
    return usuario.branches(db, idCiud)


@router.get("/usuarios/{idUsuario}", response_model=UsuarioDetalle)
def detail(idUsuario: str, db: Session = Depends(get_db)):
    return usuario.detail(db, usuario.internal(db, idUsuario))


@router.post("/empleados", response_model=UsuarioDetalle, status_code=201)
def create_employee(data: EmpleadoCrear, request: Request, db: Session = Depends(get_db),
                    identity=Depends(cu05_identity)):
    return usuario.create_employee(db, data, request.app.state.settings, request.app.state.passwords,
                                   identity.usuario.idUsuario, peer(request))


@router.patch("/empleados/{idUsuario}", response_model=UsuarioDetalle)
def edit_employee(idUsuario: str, data: EmpleadoEditar, request: Request, db: Session = Depends(get_db),
                  identity=Depends(cu05_identity)):
    return usuario.edit_employee(db, idUsuario, data, request.app.state.settings,
                                 identity.usuario.idUsuario, peer(request))


@router.patch("/usuarios/{idUsuario}/estado", response_model=UsuarioDetalle)
def set_state(idUsuario: str, data: UsuarioEstado, request: Request, db: Session = Depends(get_db),
              identity=Depends(cu05_identity)):
    return usuario.set_state(db, idUsuario, data, identity.usuario.idUsuario, peer(request), request.app.state.settings)
