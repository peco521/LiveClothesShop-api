"""Platform-authorized, read-only reports with explicit per-company predicates."""
from datetime import date, datetime
from io import BytesIO
import re
from zipfile import ZIP_DEFLATED, ZipFile
from zoneinfo import ZoneInfo

from sqlalchemy import event, text
from sqlalchemy.orm import with_loader_criteria

from app.core.database import Base, tenant_context
from app.core.errors import DomainError
from app.modules.saas_suscripciones.suscripciones.services import plataforma
from app.modules.ventas_reportes.cu25_reportes.services import dinamicos, reportes as cu25
from app.modules.ventas_reportes.cu25_reportes.services import interpretacion


MAX_COMPANIES_PER_REQUEST = 100


def empresas_con_suscripcion(db, *, idemp=None, idplan=None, hoy=None):
    hoy = hoy or datetime.now(ZoneInfo("America/La_Paz")).date()
    empresas = plataforma.listar_empresas(db)
    resultado = []
    for company in empresas:
        start, end = company.get("fechaInicio"), company.get("fechaVencimiento")
        if isinstance(start, str):
            start = date.fromisoformat(start[:10])
        if isinstance(end, str):
            end = date.fromisoformat(end[:10])
        if (company.get("suscripcionEstado") != "activa" or start is None or end is None
                or start > hoy or end < hoy):
            continue
        if idemp is not None and company["idEmp"] != idemp:
            continue
        if idplan is not None and company.get("idPlan") != idplan:
            continue
        resultado.append(company)
    if idemp is not None and not resultado:
        raise DomainError(404, "empresa_suscrita_no_encontrada",
                          "La empresa no tiene una suscripción vigente en la plataforma")
    if not resultado:
        raise DomainError(404, "sin_empresas_suscritas",
                          "No hay empresas con suscripción vigente para este filtro")
    if len(resultado) > MAX_COMPANIES_PER_REQUEST:
        raise DomainError(422, "alcance_demasiado_amplio",
                          f"El reporte admite hasta {MAX_COMPANIES_PER_REQUEST} empresas por consulta")
    return resultado


def _tenant_session(request, company):
    if not request.app.state.settings.multitenant_enabled:
        raise DomainError(503, "aislamiento_tenant_no_disponible",
                          "Los reportes SaaS requieren el aislamiento multitenant habilitado")
    factory = (getattr(request.app.state, "platform_session_factory", None)
               or getattr(request.app.state, "session_factory", None))
    if factory is None:
        raise DomainError(503, "base_empresarial_no_configurada",
                          "No se pudo abrir una sesion de lectura empresarial")
    db = factory()
    company_id = int(company["idEmp"])
    db.info["multitenant"] = True
    # These sessions are created only after platform authorization. Add explicit
    # predicates to every ORM entity/alias, including subqueries, so reports
    # remain scoped even when the platform connection has BYPASSRLS.
    criteria = [with_loader_criteria(mapper.class_, lambda entity: entity.idemp == company_id,
                                    include_aliases=True)
                for mapper in Base.registry.mappers if hasattr(mapper.class_, "idemp")]

    def restrict_report(statement):
        if statement.is_select:
            statement.statement = statement.statement.options(*criteria)

    event.listen(db, "do_orm_execute", restrict_report)
    try:
        if db.get_bind().dialect.name == "postgresql":
            db.execute(text("SET TRANSACTION READ ONLY"))
        tenant_context(db, company_id)
    except Exception:
        db.rollback()
        db.close()
        raise
    return db


def _run_company(request, company, fn):
    db = _tenant_session(request, company)
    try:
        return fn(db)
    finally:
        db.rollback()
        db.close()


def dashboard(request, companies, fecha_ini, fecha_fin, *, nro_suc=None,
              id_cat=None, id_temp=None, zone="America/La_Paz"):
    if fecha_fin < fecha_ini or (fecha_fin - fecha_ini).days > 366:
        raise DomainError(422, "rango_invalido", "El período debe ser ordenado y de hasta 367 días")
    reports = []
    for company in companies:
        data = _run_company(request, company, lambda db: cu25.dashboard(
            db, fecha_ini, fecha_fin, nro_suc, id_cat, id_temp, zone))
        # The dashboard returns aggregate indicators; detailed rows are produced
        # only through the explicit, downloadable CU25 report export.
        data.pop("reporte", None)
        data.pop("devolucionesDetalle", None)
        reports.append({"empresa": company, "reporte": data})
    return {"fechaIni": fecha_ini, "fechaFin": fecha_fin, "empresas": reports,
            "procedencia": "Datos comerciales filtrados por empresa"}


def referencias_empresa(request, company):
    def read(db):
        return [dict(row) for row in db.execute(text(
            "SELECT nro, nombre FROM sucursal WHERE idemp=:tenant AND estado='activo' ORDER BY nombre"
        ), {"tenant": company["idEmp"]}).mappings()]
    return _run_company(request, company, read)


def interpretar_voz(request, companies, texto, *, ia_enabled):
    branches = referencias_empresa(request, companies[0]) if len(companies) == 1 else []
    return interpretacion.interpret(texto, branches, enabled=ia_enabled)


def ejecutar_voz(request, companies, plan, *, actor, zone="America/La_Paz"):
    _validate_voice_scope(companies, plan)
    results = []
    for company in companies:
        data = _run_company(request, company, lambda db: dinamicos.calculate(
            db, plan, actor, zone, check_feature=False, audit=False,
            source_label=f"FashionStore SaaS · {company['nombre']}", keep_snapshot=False))
        results.append({"empresa": company, "reporte": data})
    return {"plan": plan.model_dump(mode="json"), "empresas": results,
            "procedencia": "Resultados del reporte por empresa"}


def exportar(request, companies, *, fecha_ini, fecha_fin, tipo, formato,
             nro_suc=None, id_cat=None, id_temp=None, zone="America/La_Paz",
             force_archive=False):
    if fecha_fin < fecha_ini or (fecha_fin - fecha_ini).days > 366:
        raise DomainError(422, "rango_invalido", "El período debe ser ordenado y de hasta 367 días")
    content_type = "application/pdf" if formato == "pdf" else (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    if len(companies) == 1 and not force_archive:
        model = _run_company(request, companies[0], lambda db: cu25.report_model(
            db, tipo, fecha_ini, fecha_fin, nro_suc, id_cat, id_temp, zone))
        content = cu25.pdf(model) if formato == "pdf" else cu25.excel(model)
        name = _slug(companies[0]["nombre"])
        return content, content_type, f"reporte-{name}-{tipo}.{formato}"

    archive = BytesIO()
    with ZipFile(archive, "w", ZIP_DEFLATED) as bundle:
        for company in companies:
            model = _run_company(request, company, lambda db: cu25.report_model(
                db, tipo, fecha_ini, fecha_fin, nro_suc, id_cat, id_temp, zone))
            content = cu25.pdf(model) if formato == "pdf" else cu25.excel(model)
            bundle.writestr(f"{_slug(company['nombre'])}-{company['idEmp']}/reporte-{tipo}.{formato}", content)
    return archive.getvalue(), "application/zip", f"reportes-{tipo}-{formato}.zip"


def exportar_voz(request, companies, plan, *, actor, formato, zone="America/La_Paz"):
    _validate_voice_scope(companies, plan)
    content_type = "application/pdf" if formato == "pdf" else (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    if len(companies) == 1:
        company = companies[0]
        result = _run_company(request, company, lambda db: dinamicos.calculate(
            db, plan, actor, zone, check_feature=False, audit=False,
            source_label=f"FashionStore SaaS · {company['nombre']}", keep_snapshot=False))
        model = dinamicos.export_model(result)
        content = cu25.pdf(model) if formato == "pdf" else cu25.excel(model)
        name = _slug(company["nombre"])
        return content, content_type, f"reporte-{name}-{plan.tipo}.{formato}"

    archive = BytesIO()
    with ZipFile(archive, "w", ZIP_DEFLATED) as bundle:
        for company in companies:
            result = _run_company(request, company, lambda db: dinamicos.calculate(
                db, plan, actor, zone, check_feature=False, audit=False,
                source_label=f"FashionStore SaaS · {company['nombre']}", keep_snapshot=False))
            model = dinamicos.export_model(result)
            content = cu25.pdf(model) if formato == "pdf" else cu25.excel(model)
            bundle.writestr(f"{_slug(company['nombre'])}-{company['idEmp']}/reporte-{plan.tipo}.{formato}", content)
    return archive.getvalue(), "application/zip", f"reportes-{plan.tipo}-{formato}.zip"


def _slug(value):
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(value)).strip("-")
    return (cleaned[:60] or "empresa").lower()


def _validate_voice_scope(companies, plan):
    if len(companies) > 1 and plan.nroSuc is not None:
        raise DomainError(422, "filtro_ambiguo",
                          "Selecciona una empresa para filtrar una sucursal en el reporte de IA")
