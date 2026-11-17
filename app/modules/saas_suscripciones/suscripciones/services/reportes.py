"""Platform-authorized, read-only reports with explicit per-company predicates."""
from datetime import date, datetime
from decimal import Decimal
import re
import unicodedata
from zoneinfo import ZoneInfo

from sqlalchemy import event, text
from sqlalchemy.orm import with_loader_criteria

from app.core.database import Base, tenant_context
from app.core.errors import DomainError
from app.modules.saas_suscripciones.suscripciones.services import plataforma
from app.modules.ventas_reportes.cu25_reportes.services import dinamicos, reportes as cu25
from app.modules.ventas_reportes.cu25_reportes.services import interpretacion


MAX_COMPANIES_PER_REQUEST = 100


def empresas_con_suscripcion(db, *, idemp=None, idemps=None, idplan=None, hoy=None):
    hoy = hoy or datetime.now(ZoneInfo("America/La_Paz")).date()
    empresas = plataforma.listar_empresas(db)
    wanted_ids = set(idemps) if idemps else None
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
        if wanted_ids is not None and company["idEmp"] not in wanted_ids:
            continue
        if idplan is not None and company.get("idPlan") != idplan:
            continue
        resultado.append(company)
    if (idemp is not None or wanted_ids is not None) and (
        not resultado or (wanted_ids is not None and len({c["idEmp"] for c in resultado}) != len(wanted_ids))
    ):
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


def _norm(value: str) -> str:
    stripped = unicodedata.normalize("NFKD", str(value or "").strip().lower())
    return "".join(ch for ch in stripped if not unicodedata.combining(ch))


def _detect_company_scope(all_companies, texto: str):
    """Detecta si la solicitud nombra explícitamente una empresa, varias o todas."""
    normalized = _norm(texto)
    if re.search(r"\b(?:todas\s+las\s+empresas|todas\s+empresas|nivel\s+global|de\s+todas)\b", normalized):
        return "all", []
    matched = []
    seen_ids = set()
    for company in all_companies:
        candidates = [company.get("nombre", ""), company.get("codigo", "")]
        for raw in candidates:
            token = _norm(raw)
            if len(token) < 2:
                continue
            if re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", normalized):
                if company["idEmp"] not in seen_ids:
                    seen_ids.add(company["idEmp"])
                    matched.append(company)
                break
    if matched:
        return "matched", matched
    return "inherit", []


def interpretar_voz(request, companies, texto, *, ia_enabled, all_companies=None, explicit_scope=False):
    pool = all_companies if all_companies is not None else companies
    mode, matched = _detect_company_scope(pool, texto)
    if mode == "all":
        target_companies = pool
        resolved_ids: list[int] | None = []
    elif mode == "matched":
        target_companies = matched
        resolved_ids = [c["idEmp"] for c in matched]
    else:
        target_companies = companies
        resolved_ids = [c["idEmp"] for c in companies] if explicit_scope else None

    branches = referencias_empresa(request, target_companies[0]) if len(target_companies) == 1 else []
    decision = interpretacion.interpret(texto, branches, enabled=ia_enabled)
    if decision.get("estado") == "listo":
        decision["idEmps"] = resolved_ids
        decision["empresasNombres"] = (
            [c["nombre"] for c in target_companies] if resolved_ids else []
        )
    return decision


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


def _scope_label(companies):
    names = [f"{c['nombre']} ({c['codigo']})" for c in companies[:6]]
    if len(companies) > 6:
        names.append(f"+{len(companies) - 6} más")
    return ", ".join(names)


def _consolidated_standard_model(companies, dashboards, *, fecha_ini, fecha_fin, tipo):
    filtros = [
        ("Período:", f"{fecha_ini.isoformat()} a {fecha_fin.isoformat()}"),
        ("Alcance:", f"{len(companies)} empresas: {_scope_label(companies)}"),
        ("Origen:", "PostgreSQL · Datos consolidados por empresa"),
    ]
    base_cols = [("Empresa", "texto", 26), ("Código", "texto", 14), ("Plan", "texto", 15)]
    if tipo == "ventas":
        columnas = base_cols + [
            ("Ventas pagadas", "entero", 15),
            ("Unidades vendidas", "entero", 17),
            ("Ingresos", "moneda", 16),
            ("Ticket promedio", "moneda", 16),
        ]
        filas = []
        tot_ventas, tot_unid, tot_ing = 0, 0, Decimal("0.00")
        for comp, d in zip(companies, dashboards):
            v = int(d.get("ventas") or 0)
            u = int(d.get("poleras") or 0)
            ing = Decimal(str(d.get("ingresos") or "0.00"))
            tkt = Decimal(str(d.get("ticketPromedio") or "0.00"))
            tot_ventas += v
            tot_unid += u
            tot_ing += ing
            filas.append([comp["nombre"], comp["codigo"], comp.get("plan") or "Sin plan", v, u, ing, tkt])
        avg_ticket = (tot_ing / tot_ventas).quantize(Decimal("0.01")) if tot_ventas else Decimal("0.00")
        total = ["TOTAL GENERAL", "", f"{len(companies)} emp.", tot_ventas, tot_unid, tot_ing, avg_ticket]
        titulo = "REPORTE CONSOLIDADO DE VENTAS POR EMPRESA"
    elif tipo == "inventario":
        columnas = base_cols + [
            ("Stock físico", "entero", 14),
            ("Disponible", "entero", 14),
            ("Reservado", "entero", 14),
            ("Agotados", "entero", 12),
        ]
        filas = []
        t_st, t_disp, t_res, t_ago = 0, 0, 0, 0
        for comp, d in zip(companies, dashboards):
            inv = d.get("inventario") or {}
            st = int(inv.get("stock") or 0)
            disp = int(inv.get("disponible") or 0)
            res = int(inv.get("reservado") or 0)
            ago = int(inv.get("agotados") or 0)
            t_st += st; t_disp += disp; t_res += res; t_ago += ago
            filas.append([comp["nombre"], comp["codigo"], comp.get("plan") or "Sin plan", st, disp, res, ago])
        total = ["TOTAL GENERAL", "", f"{len(companies)} emp.", t_st, t_disp, t_res, t_ago]
        titulo = "REPORTE CONSOLIDADO DE INVENTARIO POR EMPRESA"
    elif tipo == "reservas":
        columnas = base_cols + [
            ("Reservas", "entero", 14),
            ("Conv. reservas %", "entero", 16),
            ("Carritos", "entero", 14),
        ]
        filas = []
        t_res, t_car = 0, 0
        for comp, d in zip(companies, dashboards):
            r = int(d.get("reservas") or 0)
            conv = int(round(float(d.get("conversionReservas") or 0)))
            car = int(d.get("carritos") or 0)
            t_res += r; t_car += car
            filas.append([comp["nombre"], comp["codigo"], comp.get("plan") or "Sin plan", r, conv, car])
        total = ["TOTAL GENERAL", "", f"{len(companies)} emp.", t_res, "", t_car]
        titulo = "REPORTE CONSOLIDADO DE RESERVAS POR EMPRESA"
    else:
        columnas = base_cols + [
            ("Devoluciones", "entero", 14),
            ("Ventas pagadas", "entero", 15),
            ("Ingresos", "moneda", 16),
        ]
        filas = []
        t_dev, t_ven, t_ing = 0, 0, Decimal("0.00")
        for comp, d in zip(companies, dashboards):
            dev = int(d.get("devoluciones") or 0)
            ven = int(d.get("ventas") or 0)
            ing = Decimal(str(d.get("ingresos") or "0.00"))
            t_dev += dev; t_ven += ven; t_ing += ing
            filas.append([comp["nombre"], comp["codigo"], comp.get("plan") or "Sin plan", dev, ven, ing])
        total = ["TOTAL GENERAL", "", f"{len(companies)} emp.", t_dev, t_ven, t_ing]
        titulo = "REPORTE CONSOLIDADO DE DEVOLUCIONES POR EMPRESA"
    return {
        "titulo": titulo,
        "filtros": filtros,
        "columnas": columnas,
        "filas": filas,
        "total": total,
        "hojas": [],
        "nota": "Consolidado multiempresa: cada fila representa una empresa con su suscripción vigente.",
    }


def _consolidated_dynamic_model(companies, results, *, plan):
    numeric_cols = {"ventas", "unidades", "stock_fisico", "disponible"}
    source_cols = [c for c in results[0]["columnas"] if c not in ("sucursal_id", "producto_id")]
    col_defs = [("Empresa", "texto", 28)] + [
        (key.replace("_", " ").title(), "moneda" if key == "importe" else "entero" if key in numeric_cols else "texto", 22)
        for key in source_cols
    ]
    filas = []
    totals = {key: (Decimal("0.00") if key == "importe" else 0) for key in source_cols if key in numeric_cols or key == "importe"}
    for comp, res in zip(companies, results):
        company_label = f"{comp['nombre']} ({comp['codigo']})"
        rows = res.get("filas") or []
        if not rows:
            empty_row = [company_label]
            for idx, key in enumerate(source_cols):
                if key == "importe":
                    empty_row.append(Decimal("0.00"))
                elif key in numeric_cols:
                    empty_row.append(0)
                elif idx == 0:
                    empty_row.append("Sin registros en el período")
                else:
                    empty_row.append("—")
            filas.append(empty_row)
            continue
        for r in rows:
            line = [company_label]
            for key in source_cols:
                val = r.get(key)
                if key == "importe":
                    dec = Decimal(str(val or "0.00"))
                    totals[key] += dec
                    line.append(dec)
                elif key in numeric_cols:
                    num = int(val or 0)
                    totals[key] += num
                    line.append(num)
                else:
                    line.append(val if val is not None else "—")
            filas.append(line)

    total_row = ["TOTAL GENERAL"] + [
        totals[key] if key in totals else ""
        for key in source_cols
    ]
    periodo_txt = f"{plan.fechaIni.isoformat()} a {plan.fechaFin.isoformat()}" if plan.fechaIni and plan.fechaFin else "Existencias actuales"
    return {
        "titulo": f"{dinamicos.TITLES[plan.tipo].upper()} · CONSOLIDADO",
        "filtros": [
            ("Período:", periodo_txt),
            ("Alcance:", f"{len(companies)} empresas: {_scope_label(companies)}"),
            ("Origen:", "PostgreSQL · Datos consolidados por empresa"),
        ],
        "columnas": col_defs,
        "filas": filas,
        "total": total_row,
        "hojas": [],
        "nota": f"Consolidado multiempresa ({len(companies)} empresas) generado en un único documento.",
    }


def exportar(request, companies, *, fecha_ini, fecha_fin, tipo, formato,
             nro_suc=None, id_cat=None, id_temp=None, zone="America/La_Paz",
             force_archive=False):
    if fecha_fin < fecha_ini or (fecha_fin - fecha_ini).days > 366:
        raise DomainError(422, "rango_invalido", "El período debe ser ordenado y de hasta 367 días")
    content_type = "application/pdf" if formato == "pdf" else (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    if len(companies) == 1:
        model = _run_company(request, companies[0], lambda db: cu25.report_model(
            db, tipo, fecha_ini, fecha_fin, nro_suc, id_cat, id_temp, zone))
        content = cu25.pdf(model) if formato == "pdf" else cu25.excel(model)
        name = _slug(companies[0]["nombre"])
        return content, content_type, f"reporte-{name}-{tipo}.{formato}"

    dashboards = [
        _run_company(request, company, lambda db: cu25.dashboard(
            db, fecha_ini, fecha_fin, nro_suc, id_cat, id_temp, zone))
        for company in companies
    ]
    model = _consolidated_standard_model(companies, dashboards, fecha_ini=fecha_ini, fecha_fin=fecha_fin, tipo=tipo)
    content = cu25.pdf(model) if formato == "pdf" else cu25.excel(model)
    return content, content_type, f"reporte-consolidado-{tipo}.{formato}"


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

    results = [
        _run_company(request, company, lambda db: dinamicos.calculate(
            db, plan, actor, zone, check_feature=False, audit=False,
            source_label=f"FashionStore SaaS · {company['nombre']}", keep_snapshot=False))
        for company in companies
    ]
    model = _consolidated_dynamic_model(companies, results, plan=plan)
    content = cu25.pdf(model) if formato == "pdf" else cu25.excel(model)
    return content, content_type, f"reporte-consolidado-{plan.tipo}.{formato}"


def _slug(value):
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(value)).strip("-")
    return (cleaned[:60] or "empresa").lower()


def _validate_voice_scope(companies, plan):
    if len(companies) > 1 and plan.nroSuc is not None:
        raise DomainError(422, "filtro_ambiguo",
                          "Selecciona una empresa para filtrar una sucursal en el reporte de IA")
