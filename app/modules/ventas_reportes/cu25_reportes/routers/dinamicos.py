from uuid import UUID
from typing import Literal
from fastapi import APIRouter, Depends, Request, Response
from app.core.database import get_db
from app.core.dependencies import require_permission
from app.core.errors import DomainError
from ..schemas.dinamicos import ReportPlan, InterpretRequest, PredictionRequest
from ..services import dinamicos as service, interpretacion, reportes

router = APIRouter(prefix='/api/admin/reportes', tags=['CU25 dinámicos'])

def authorized(request: Request, identity=Depends(require_permission('CU25')), db=Depends(get_db)):
    if any(key.lower() in {'empresa','idemp','tenant','idusuario'} for key in request.query_params) or any(
            key in request.headers for key in ('x-empresa','x-idemp','x-tenant-id')):
        raise DomainError(422,'datos_invalidos','La empresa y el usuario provienen de sesión')
    return service.actor_scope(db, identity)

@router.get('/dinamicos/referencias')
def references(db=Depends(get_db), actor=Depends(authorized)):
    return {'sucursales':service.references(db,actor),'tipos':service.TITLES}

@router.post('/dinamicos/ejecutar')
def execute(plan: ReportPlan, request: Request, db=Depends(get_db), actor=Depends(authorized)):
    return service.calculate(db, plan, actor, request.app.state.settings.reportes_zona_horaria)

@router.get('/dinamicos/{result_id}/exportar')
def export(result_id: UUID, formato: Literal['pdf','xlsx'], db=Depends(get_db), actor=Depends(authorized)):
    model = service.export_model(service.snapshot(db,result_id,actor))
    content = reportes.pdf(model) if formato=='pdf' else reportes.excel(model)
    media = 'application/pdf' if formato=='pdf' else 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    return Response(content,media_type=media,headers={'Content-Disposition':f'attachment; filename="fashionstore-reporte.{formato}"'})

@router.post('/interpretar')
def interpret(input: InterpretRequest, request: Request, db=Depends(get_db), actor=Depends(authorized)):
    service.feature(db,'REPORTES_IA')
    branches = service.references(db,actor)
    return interpretacion.interpret(input.texto,branches,enabled=request.app.state.settings.reportes_ia_local_enabled)

@router.get('/analitica')
def analytics(db=Depends(get_db), actor=Depends(authorized)):
    service.feature(db,'REPORTES_IA')
    from ..services.analitica_ml import summary
    return summary()

@router.post('/analitica/predecir')
def predict(input: PredictionRequest, db=Depends(get_db), actor=Depends(authorized)):
    service.feature(db,'REPORTES_IA')
    from ..services.analitica_ml import predict
    try:
        return predict(input.model_dump())
    except ValueError:
        raise DomainError(422,'datos_invalidos','Revisa los valores del escenario sintético') from None
    except RuntimeError:
        raise DomainError(503,'modelo_ml_no_disponible','Entrena el artefacto local compatible antes de predecir') from None
