from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session

from app.core.database import get_platform_db
from app.modules.saas_suscripciones.shared.dependencies import require_platform_permission
from app.modules.saas_suscripciones.suscripciones.schemas.reportes import (
    SaasVoiceExecutionRequest,
    SaasVoiceExportRequest,
    SaasVoiceInterpretRequest,
)
from app.modules.ventas_reportes.cu25_reportes.schemas.dinamicos import PredictionRequest
from app.core.errors import DomainError
from app.modules.saas_suscripciones.suscripciones.services import reportes


router = APIRouter(prefix="/api/saas/reportes", tags=["Reportes SaaS"])
EMPRESAS = "SAAS-EMPRESAS"


def _companies(db, idEmp=None, idPlan=None):
    return reportes.empresas_con_suscripcion(db, idemp=idEmp, idplan=idPlan)


def _validate_local_filters(companies, nroSuc, idCat, idTemp):
    if len(companies) > 1 and any(value is not None for value in (nroSuc, idCat, idTemp)):
        raise DomainError(422, "filtro_ambiguo",
                          "Sucursal, modelo y temporada solo se pueden filtrar al elegir una empresa")


@router.get("/estado")
def estado_reportes(request: Request,
                    identity=Depends(require_platform_permission(EMPRESAS))):
    return {"iaDisponible": request.app.state.settings.reportes_ia_local_enabled,
            "tipos": ["ventas", "inventario", "reservas", "devoluciones"],
            "maxEmpresasPorConsulta": reportes.MAX_COMPANIES_PER_REQUEST}


@router.get("/empresas")
def empresas(db: Session = Depends(get_platform_db),
             identity=Depends(require_platform_permission(EMPRESAS))):
    return _companies(db)


@router.get("/dashboard")
def dashboard(fechaIni: date, fechaFin: date, request: Request,
              idEmp: int | None = Query(None, ge=1), idPlan: int | None = Query(None, ge=1),
              nroSuc: int | None = Query(None, ge=1), idCat: int | None = Query(None, ge=1),
              idTemp: int | None = Query(None, ge=1),
              db: Session = Depends(get_platform_db),
              identity=Depends(require_platform_permission(EMPRESAS))):
    selected = _companies(db, idEmp, idPlan)
    _validate_local_filters(selected, nroSuc, idCat, idTemp)
    return reportes.dashboard(request, selected, fechaIni, fechaFin, nro_suc=nroSuc,
                              id_cat=idCat, id_temp=idTemp,
                              zone=request.app.state.settings.reportes_zona_horaria)


@router.get("/exportar")
def exportar(tipo: Literal["ventas", "inventario", "reservas", "devoluciones"],
             formato: Literal["pdf", "xlsx"], fechaIni: date, fechaFin: date, request: Request,
             idEmp: int | None = Query(None, ge=1), idPlan: int | None = Query(None, ge=1),
             nroSuc: int | None = Query(None, ge=1), idCat: int | None = Query(None, ge=1),
             idTemp: int | None = Query(None, ge=1),
             db: Session = Depends(get_platform_db),
             identity=Depends(require_platform_permission(EMPRESAS))):
    selected = _companies(db, idEmp, idPlan)
    _validate_local_filters(selected, nroSuc, idCat, idTemp)
    content, media, filename = reportes.exportar(
        request, selected, fecha_ini=fechaIni, fecha_fin=fechaFin,
        tipo=tipo, formato=formato, nro_suc=nroSuc, id_cat=idCat, id_temp=idTemp,
        zone=request.app.state.settings.reportes_zona_horaria,
        force_archive=idEmp is None)
    return Response(content, media_type=media,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.post("/interpretar")
def interpretar(data: SaasVoiceInterpretRequest, request: Request,
                db: Session = Depends(get_platform_db),
                identity=Depends(require_platform_permission(EMPRESAS))):
    selected = _companies(db, data.idEmp, data.idPlan)
    return reportes.interpretar_voz(request, selected, data.texto,
                                    ia_enabled=request.app.state.settings.reportes_ia_local_enabled)


@router.get("/referencias")
def referencias(request: Request, idEmp: int | None = Query(None, ge=1),
                db: Session = Depends(get_platform_db),
                identity=Depends(require_platform_permission(EMPRESAS))):
    sucursales = []
    if idEmp is not None:
        selected = _companies(db, idEmp=idEmp)
        if selected:
            sucursales = reportes.referencias_empresa(request, selected[0])
    from app.modules.ventas_reportes.cu25_reportes.services.dinamicos import TITLES
    return {"sucursales": sucursales, "tipos": TITLES}


@router.post("/dinamicos/ejecutar")
def ejecutar_dinamico(data: SaasVoiceExecutionRequest, request: Request,
                      db: Session = Depends(get_platform_db),
                      identity=Depends(require_platform_permission(EMPRESAS))):
    selected = _companies(db, data.idEmp, data.idPlan)
    actor = (identity.usuario.idUsuario, None)
    return reportes.ejecutar_voz(request, selected, data.plan, actor=actor,
                                 zone=request.app.state.settings.reportes_zona_horaria)


@router.post("/dinamicos/exportar")
def exportar_dinamico(data: SaasVoiceExportRequest, request: Request,
                      db: Session = Depends(get_platform_db),
                      identity=Depends(require_platform_permission(EMPRESAS))):
    selected = _companies(db, data.idEmp, data.idPlan)
    content, media, filename = reportes.exportar_voz(
        request, selected, data.plan, actor=(identity.usuario.idUsuario, None),
        formato=data.formato, zone=request.app.state.settings.reportes_zona_horaria)
    return Response(content, media_type=media,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/analitica")
def analitica_saas(identity=Depends(require_platform_permission(EMPRESAS))):
    from app.modules.ventas_reportes.cu25_reportes.services.analitica_ml import summary
    return summary()


@router.post("/analitica/predecir")
def predecir_saas(input: PredictionRequest,
                  identity=Depends(require_platform_permission(EMPRESAS))):
    from app.modules.ventas_reportes.cu25_reportes.services.analitica_ml import predict
    try:
        return predict(input.model_dump())
    except ValueError:
        raise DomainError(422, "datos_invalidos", "Revisa los valores del escenario sintético") from None
    except RuntimeError:
        raise DomainError(503, "modelo_ml_no_disponible", "Entrena el artefacto local compatible antes de predecir") from None


@router.post("/voz/ejecutar")
def ejecutar_voz(data: SaasVoiceExecutionRequest, request: Request,
                 db: Session = Depends(get_platform_db),
                 identity=Depends(require_platform_permission(EMPRESAS))):
    selected = _companies(db, data.idEmp, data.idPlan)
    actor = (identity.usuario.idUsuario, None)
    return reportes.ejecutar_voz(request, selected, data.plan, actor=actor,
                                 zone=request.app.state.settings.reportes_zona_horaria)


@router.post("/voz/exportar")
def exportar_voz(data: SaasVoiceExportRequest, request: Request,
                 db: Session = Depends(get_platform_db),
                 identity=Depends(require_platform_permission(EMPRESAS))):
    selected = _companies(db, data.idEmp, data.idPlan)
    content, media, filename = reportes.exportar_voz(
        request, selected, data.plan, actor=(identity.usuario.idUsuario, None),
        formato=data.formato, zone=request.app.state.settings.reportes_zona_horaria)
    return Response(content, media_type=media,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})
