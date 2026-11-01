from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import current_identity
from app.core.errors import DomainError
from app.modules.sincronizacion import services
from app.modules.sincronizacion.schemas import (
    HistorialSync, LoteSync, ResolucionConflicto, ResultadoLote,
)

router = APIRouter(prefix="/api/sync", tags=["Sincronización offline"])


def _require_multitenant(request):
    if not request.app.state.settings.multitenant_enabled:
        raise DomainError(503, "sincronizacion_no_disponible",
                          "La sincronización offline requiere el modo multitenant")


@router.post("/lote", response_model=ResultadoLote)
def enviar_lote(data: LoteSync, request: Request, db: Session = Depends(get_db),
                identity=Depends(current_identity)):
    """Recibe un lote acotado y devuelve un resultado por operación.

    La empresa se toma de la sesión autenticada; cualquier idEmp del cuerpo se
    rechaza por el esquema (`extra="forbid"`).
    """
    _require_multitenant(request)
    return services.procesar(db, data, request.app.state.settings, identity)


@router.get("/operaciones", response_model=HistorialSync)
def historial(estado: str | None = Query(default=None, pattern=r"^[a-z_]+$"),
              offset: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=1, le=200),
              request: Request = None, db: Session = Depends(get_db),
              identity=Depends(current_identity)):
    _require_multitenant(request)
    return services.historial(db, estado, offset, limit)


@router.post("/conflictos/{operacion_id}/resolver")
def resolver_conflicto(operacion_id: UUID, data: ResolucionConflicto, request: Request,
                       db: Session = Depends(get_db), identity=Depends(current_identity)):
    _require_multitenant(request)
    return services.resolver_conflicto(db, operacion_id, data.detalle, identity)
