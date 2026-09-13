from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import require_permission
from app.modules.seguridad_accesos.cu09_sucursales_ciudades.schemas.organizacion import (
    CiudadCrear, CiudadDetalle, CiudadEditar, CiudadesFiltros, CiudadesListado,
    SucursalCrear, SucursalDetalle, SucursalEditar, SucursalesFiltros, SucursalesListado,
)
from app.modules.seguridad_accesos.cu09_sucursales_ciudades.services import organizacion as service

cu09_identity = require_permission("CU09")
router = APIRouter(prefix="/api/admin", tags=["CU09 Sucursales y Ciudades"],
                   dependencies=[Depends(cu09_identity)])
CityPath = Annotated[int, Path(ge=-32768, le=32767)]
BranchPath = Annotated[int, Path(ge=-2147483648, le=2147483647)]


def peer(request):
    return request.client.host if request.client else None


@router.get("/ciudades", response_model=CiudadesListado)
def list_cities(filters: Annotated[CiudadesFiltros, Query()], db: Session = Depends(get_db)):
    return service.list_cities(db, filters)


@router.post("/ciudades", response_model=CiudadDetalle, status_code=201)
def create_city(data: CiudadCrear, request: Request, db: Session = Depends(get_db), identity=Depends(cu09_identity)):
    return service.create_city(db, data, identity.usuario.idUsuario, peer(request))


@router.get("/ciudades/{id}", response_model=CiudadDetalle)
def detail_city(id: CityPath, db: Session = Depends(get_db)):
    return service.city_detail(service.city(db, id))


@router.patch("/ciudades/{id}", response_model=CiudadDetalle)
def edit_city(id: CityPath, data: CiudadEditar, request: Request, db: Session = Depends(get_db), identity=Depends(cu09_identity)):
    return service.edit_city(db, id, data, identity.usuario.idUsuario, peer(request))


@router.get("/sucursales", response_model=SucursalesListado)
def list_branches(filters: Annotated[SucursalesFiltros, Query()], db: Session = Depends(get_db)):
    return service.list_branches(db, filters)


@router.post("/sucursales", response_model=SucursalDetalle, status_code=201)
def create_branch(data: SucursalCrear, request: Request, db: Session = Depends(get_db), identity=Depends(cu09_identity)):
    return service.create_branch(db, data, identity.usuario.idUsuario, peer(request))


@router.get("/sucursales/{nro}", response_model=SucursalDetalle)
def detail_branch(nro: BranchPath, db: Session = Depends(get_db)):
    return service.detail_branch(db, nro)


@router.patch("/sucursales/{nro}", response_model=SucursalDetalle)
def edit_branch(nro: BranchPath, data: SucursalEditar, request: Request, db: Session = Depends(get_db), identity=Depends(cu09_identity)):
    return service.edit_branch(db, nro, data, identity.usuario.idUsuario, peer(request))
