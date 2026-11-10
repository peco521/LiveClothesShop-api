from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.database import get_platform_db
from app.modules.saas_suscripciones.shared.dependencies import require_platform_permission
from app.modules.saas_suscripciones.planes_funcionalidades.services import planes as service
from app.modules.saas_suscripciones.planes_funcionalidades.schemas.planes import PlanActualizacion, PlanDatos

router = APIRouter(prefix='/api/saas/planes', tags=['SaaS · Planes y funcionalidades'])


platform_admin = require_platform_permission("SAAS-PLANES")


def peer(request: Request):
    return request.client.host if request.client else None


@router.get('')
def plans(db: Session = Depends(get_platform_db), actor=Depends(platform_admin)):
    return service.list_plans(db)


@router.get('/funcionalidades')
def features(db: Session = Depends(get_platform_db), actor=Depends(platform_admin)):
    return service.list_features(db)


@router.post('', status_code=201)
def create_plan(data: PlanDatos, request: Request, db: Session = Depends(get_platform_db),
                actor=Depends(platform_admin)):
    return service.create(db, data, actor.usuario.idUsuario, peer(request))


@router.get('/{plan_id}/suscripciones')
def plan_subscriptions(plan_id: int, db: Session = Depends(get_platform_db),
                       actor=Depends(platform_admin)):
    return service.subscriptions(db, plan_id)


@router.post('/{plan_id}/impacto')
def plan_impact(plan_id: int, data: PlanActualizacion, db: Session = Depends(get_platform_db),
                actor=Depends(platform_admin)):
    return service.preview(db, plan_id, data)


@router.put('/{plan_id}')
def update_plan(plan_id: int, data: PlanActualizacion, request: Request,
                db: Session = Depends(get_platform_db), actor=Depends(platform_admin)):
    return service.update(db, plan_id, data, actor.usuario.idUsuario, peer(request))
