import json
from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.core.database import get_platform_db
from app.core.errors import DomainError
from app.modules.plataforma_saas.backups import PlatformBackupManager
from app.modules.plataforma_saas import services
from app.modules.plataforma_saas.dependencies import require_platform_permission
from app.modules.plataforma_saas.schemas import (
    AccesoApoyo, CambioEstado, CambioPlan, EmpresaResumen, IdentidadVisual,
    NuevaEmpresa, UsoSucursales,
)

router = APIRouter(prefix="/api/saas", tags=["Plataforma SaaS"])

EMPRESAS = "SAAS-EMPRESAS"
PLANES = "SAAS-PLANES"
AUDITORIA = "SAAS-AUDITORIA"
DEMO = "SAAS-DEMO"
SOPORTE = "SAAS-SOPORTE"
RESPALDOS = "SAAS-RESPALDOS"


def _peer(request):
    return request.client.host if request.client else None


def _credentials_path(request) -> Path:
    configured = request.app.state.settings.demo_credentials_file.strip()
    if configured:
        return Path(configured).expanduser().resolve()
    root = Path(__file__).resolve().parents[3]
    return root / ".local" / "demo_credentials.json"


@router.get("/empresas", response_model=list[EmpresaResumen])
def listar_empresas(db: Session = Depends(get_platform_db),
                    identity=Depends(require_platform_permission(EMPRESAS))):
    return services.listar_empresas(db)


@router.get("/resumen")
def resumen_plataforma(db: Session = Depends(get_platform_db),
                       identity=Depends(require_platform_permission(EMPRESAS))):
    return services.obtener_resumen(db, AUDITORIA in identity.permisos)


@router.get("/empresas/{idemp}", response_model=EmpresaResumen)
def obtener_empresa(idemp: int, db: Session = Depends(get_platform_db),
                    identity=Depends(require_platform_permission(EMPRESAS))):
    return services.obtener_empresa(db, idemp)


@router.post("/empresas", response_model=EmpresaResumen, status_code=201)
def crear_empresa(data: NuevaEmpresa, request: Request, db: Session = Depends(get_platform_db),
                  identity=Depends(require_platform_permission(EMPRESAS))):
    idemp = services.crear_empresa(db, data, request.app.state.passwords,
                                   identity.usuario.idUsuario, _peer(request))
    return services.obtener_empresa(db, idemp)


@router.patch("/empresas/{idemp}/identidad", response_model=EmpresaResumen)
def actualizar_identidad(idemp: int, data: IdentidadVisual, request: Request,
                         db: Session = Depends(get_platform_db),
                         identity=Depends(require_platform_permission(EMPRESAS))):
    if data.nombre is None and data.logoUrl is None and data.colorPrimario is None and data.colorSecundario is None:
        raise DomainError(400, "sin_cambios", "No se envió ningún cambio de identidad visual")
    return services.actualizar_identidad(db, idemp, data, identity.usuario.idUsuario, _peer(request))


@router.post("/empresas/{idemp}/estado", response_model=EmpresaResumen)
def cambiar_estado(idemp: int, data: CambioEstado, request: Request,
                   db: Session = Depends(get_platform_db),
                   identity=Depends(require_platform_permission(EMPRESAS))):
    return services.cambiar_estado(db, idemp, data.estado, identity.usuario.idUsuario, _peer(request))


@router.post("/empresas/{idemp}/suscripcion", response_model=EmpresaResumen)
def cambiar_plan(idemp: int, data: CambioPlan, request: Request,
                 db: Session = Depends(get_platform_db),
                 identity=Depends(require_platform_permission(PLANES))):
    if data.fechaVencimiento <= data.fechaInicio:
        raise DomainError(400, "rango_invalido", "La fecha de vencimiento debe ser posterior al inicio")
    return services.cambiar_plan(db, idemp, data, identity.usuario.idUsuario, _peer(request))


@router.get("/empresas/{idemp}/uso-sucursales", response_model=UsoSucursales)
def uso_sucursales(idemp: int, db: Session = Depends(get_platform_db),
                   identity=Depends(require_platform_permission(EMPRESAS))):
    return services.uso_sucursales(db, idemp)


@router.get("/auditoria")
def listar_auditoria(limite: int = Query(default=100, ge=1, le=500),
                     desplazamiento: int = Query(default=0, ge=0, le=100000),
                     accion: str | None = Query(default=None, min_length=1, max_length=150),
                     db: Session = Depends(get_platform_db),
                     identity=Depends(require_platform_permission(AUDITORIA))):
    return services.listar_auditoria(db, limite, desplazamiento, accion)


@router.post("/soporte/acceso")
def acceso_soporte(data: AccesoApoyo, request: Request, db: Session = Depends(get_platform_db),
                   identity=Depends(require_platform_permission(SOPORTE))):
    return services.resumen_soporte(db, data.idEmp, data.motivo,
                                    identity.usuario.idUsuario, _peer(request))


@router.get("/demo/credenciales")
def credenciales_demo(request: Request,
                      identity=Depends(require_platform_permission(DEMO))):
    """Credenciales iniciales de las cuentas de demostración.

    Solo existe con DEMO_MODE habilitado y sobre el archivo local excluido de
    Git. No hay endpoint público: exige sesión de plataforma y permiso SAAS-DEMO.
    """
    settings = request.app.state.settings
    if not settings.demo_mode:
        raise DomainError(404, "no_disponible", "No disponible")
    path = _credentials_path(request)
    if not path.exists():
        raise DomainError(404, "credenciales_no_generadas",
                          "Ejecute scripts/seed_demo.py para generar las credenciales")
    try:
        contenido = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise DomainError(503, "credenciales_ilegibles",
                          "El archivo local de credenciales no se pudo leer") from None
    return contenido


@router.get("/respaldos")
def listar_respaldos(request: Request,
                     identity=Depends(require_platform_permission(RESPALDOS))):
    manager: PlatformBackupManager = request.app.state.saas_backup_manager
    return {"configuracion": manager.configuration(), "respaldos": manager.list_jobs()}


@router.post("/respaldos", status_code=202)
def solicitar_respaldo(request: Request, db: Session = Depends(get_platform_db),
                       identity=Depends(require_platform_permission(RESPALDOS))):
    manager: PlatformBackupManager = request.app.state.saas_backup_manager
    job = manager.prepare_job()
    try:
        with db.begin():
            services.auditar(
                db, identity.usuario.idUsuario, "respaldo_plataforma_solicitado",
                {"idRespaldo": job["id"], "destino": "fashionstore_test"}, _peer(request),
            )
    except Exception:
        manager.abort_job(job["id"])
        raise
    return manager.start_job(job["id"])


@router.get("/respaldos/{backup_id}")
def obtener_respaldo(backup_id: str, request: Request,
                     identity=Depends(require_platform_permission(RESPALDOS))):
    manager: PlatformBackupManager = request.app.state.saas_backup_manager
    return manager.get_job(backup_id)


@router.get("/respaldos/{backup_id}/descarga")
def descargar_respaldo(backup_id: str, request: Request,
                       db: Session = Depends(get_platform_db),
                       identity=Depends(require_platform_permission(RESPALDOS))):
    manager: PlatformBackupManager = request.app.state.saas_backup_manager
    path = manager.download_path(backup_id)
    with db.begin():
        services.auditar(
            db, identity.usuario.idUsuario, "respaldo_plataforma_descargado",
            {"idRespaldo": backup_id}, _peer(request),
        )
    return FileResponse(
        path, media_type="application/octet-stream",
        filename=f"fashionstore-backup-{backup_id}.dump",
    )
