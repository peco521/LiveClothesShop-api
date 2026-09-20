from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.modules.cliente_experiencia_compra.cu17_Recomendacion.schemas.recomendacion import (
    RecomendacionesRespuesta,
)
from app.modules.cliente_experiencia_compra.cu17_Recomendacion.services import (
    recomendacion as service,
)
from app.modules.cliente_experiencia_compra.shared.dependencies import require_cliente

router = APIRouter(
    prefix="/api/cliente/recomendaciones",
    tags=["CU17 Recomendaciones IA"],
    dependencies=[Depends(require_cliente)],
)


@router.get("", response_model=RecomendacionesRespuesta)
def list_recommendations(
    limit: int = Query(service.RECOMENDACIONES_POR_DEFECTO,
                       ge=service.RECOMENDACIONES_MINIMAS, le=service.RECOMENDACIONES_MAXIMAS),
    db: Session = Depends(get_db),
    identity=Depends(require_cliente),
):
    # El cliente se toma siempre de la sesión: nunca se acepta un id del cliente
    # por parámetro, para que nadie consulte recomendaciones de terceros.
    return service.recomendar(db, identity.usuario.idUsuario, limit)
