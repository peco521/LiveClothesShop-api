"""Consulta interna CU15, siempre de solo lectura y limitada por sucursal."""
from typing import Annotated
import unicodedata

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import require_permission
from app.core.errors import DomainError
from app.modules.seguridad_accesos.models import Cliente, Usuario
from app.modules.seguridad_accesos.cu05_usuarios_empleados.models.empleado import Empleado
from app.modules.cliente_experiencia_compra.shared.models.comercio import Venta
from app.modules.cliente_experiencia_compra.cu15_historial_compra.schemas.historial import CompraDetalle, HistorialCompras
from app.modules.cliente_experiencia_compra.cu15_historial_compra.services import historial as service


def internal_scope(identity=Depends(require_permission("CU15")), db: Session = Depends(get_db)):
    user = db.get(Usuario, identity.usuario.idUsuario)
    if user is not None and user.tipo == "A":
        return None
    employee = db.get(Empleado, user.idusuario) if user is not None and user.tipo == "E" else None
    cargo = "".join(c for c in unicodedata.normalize("NFD", employee.cargo.lower())
                    if not unicodedata.combining(c)).strip() if employee else ""
    if employee is None or not (cargo in {"cajero", "cajera", "encargado", "encargada"}
                               or cargo.startswith(("cajero ", "cajera ", "encargado ", "encargada "))):
        raise DomainError(403, "acceso_denegado", "No tiene autorización para consultar historiales internos")
    return employee.nrosuc


Texto = Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)]


class Filtros(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ci: Texto = ""
    nombre: Texto = ""
    apellidos: Texto = ""
    correo: Texto = ""
    offset: int = Field(0, ge=0)
    limit: int = Field(20, ge=1, le=100)


class ClienteResumen(BaseModel):
    idUsuario: str
    ci: str
    nombre: str
    apellidoPat: str
    apellidoMat: str
    correo: str


class ClientesListado(BaseModel):
    items: list[ClienteResumen]
    total: int
    offset: int
    limit: int


router = APIRouter(prefix="/api/admin/historial-compras", tags=["CU15 Historial interno"],
                   dependencies=[Depends(internal_scope)])


def permitted_customer(db, user_id, branch_id):
    query = select(Usuario).join(Cliente, Cliente.idusuario == Usuario.idusuario).where(
        Usuario.idusuario == user_id, Usuario.tipo == "C")
    if branch_id is not None:
        query = query.where(select(Venta.nroventa).where(Venta.idusuariocl == Usuario.idusuario,
                                                        Venta.nrosuc == branch_id).exists())
    if db.scalar(query) is None:
        raise DomainError(404, "cliente_no_encontrado", "Cliente no encontrado")


@router.get("/clientes", response_model=ClientesListado)
def search_customers(filters: Annotated[Filtros, Query()], db: Session = Depends(get_db), branch_id=Depends(internal_scope)):
    conditions = [Usuario.tipo == "C"]
    fields = {"ci": Usuario.ci, "nombre": Usuario.nombre,
              "apellidos": Usuario.apellidopat + " " + Usuario.apellidomat, "correo": Usuario.correo}
    for name, column in fields.items():
        value = getattr(filters, name)
        if value:
            conditions.append(func.lower(column).contains(value.lower(), autoescape=True))
    if branch_id is not None:
        conditions.append(select(Venta.nroventa).where(Venta.idusuariocl == Usuario.idusuario,
                                                       Venta.nrosuc == branch_id).exists())
    query = select(Usuario).join(Cliente, Cliente.idusuario == Usuario.idusuario).where(*conditions)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = db.scalars(query.order_by(Usuario.nombre, Usuario.apellidopat, Usuario.idusuario)
                      .offset(filters.offset).limit(filters.limit)).all()
    return ClientesListado(items=[ClienteResumen(idUsuario=u.idusuario, ci=u.ci, nombre=u.nombre,
                            apellidoPat=u.apellidopat, apellidoMat=u.apellidomat, correo=u.correo) for u in rows],
                            total=total, offset=filters.offset, limit=filters.limit)


@router.get("/clientes/{user_id}", response_model=HistorialCompras, response_model_exclude_none=True)
def customer_history(user_id: Annotated[str, Path(min_length=1, max_length=100)], offset: int = Query(0, ge=0),
                     limit: int = Query(20, ge=1, le=100), db: Session = Depends(get_db), branch_id=Depends(internal_scope)):
    permitted_customer(db, user_id, branch_id)
    return service.listar(db, user_id, offset, limit, branch_id=branch_id)


@router.get("/clientes/{user_id}/compras/{nro_venta}", response_model=CompraDetalle, response_model_exclude_none=True)
def customer_purchase(user_id: Annotated[str, Path(min_length=1, max_length=100)], nro_venta: Annotated[int, Path(ge=1, le=2147483647)],
                      db: Session = Depends(get_db), branch_id=Depends(internal_scope)):
    permitted_customer(db, user_id, branch_id)
    return service.detalle(db, nro_venta, user_id, branch_id=branch_id)
