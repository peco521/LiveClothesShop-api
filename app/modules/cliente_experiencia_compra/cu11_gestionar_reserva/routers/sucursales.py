"""Sucursales y horarios para el cliente (referencia de CU11, solo lectura)."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.modules.cliente_experiencia_compra.cu11_gestionar_reserva.schemas.reserva import (
    HorariosSucursal,
    SucursalesClienteListado,
)
from app.modules.cliente_experiencia_compra.cu11_gestionar_reserva.services import reserva as service
from app.modules.cliente_experiencia_compra.shared.dependencies import require_cliente

router = APIRouter(prefix="/api/cliente/sucursales", tags=["Reservas"],
                   dependencies=[Depends(require_cliente)])


@router.get("", response_model=SucursalesClienteListado)
def list_branches(db: Session = Depends(get_db)):
    return service.sucursales_cliente(db)


@router.get("/{nroSuc}/horarios", response_model=HorariosSucursal)
def branch_hours(nroSuc: int, db: Session = Depends(get_db)):
    return service.horarios_cliente(db, nroSuc)
