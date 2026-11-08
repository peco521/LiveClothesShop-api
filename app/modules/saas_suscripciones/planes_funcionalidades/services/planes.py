import hashlib
import json
from datetime import datetime
from ipaddress import ip_address
from zoneinfo import ZoneInfo

from sqlalchemy import Integer, String, column, delete, func, select, table, text

from app.core.errors import DomainError
from app.modules.inventario_productos.shared.access import transaction
from app.modules.saas_suscripciones.planes_funcionalidades.models.planes import (
    FuncionalidadSaas, Plan, PlanFuncionalidad, PlanVersion, PlanVersionFuncionalidad,
    SuscripcionPlan, SuscripcionVersion,
)
from app.modules.seguridad_accesos.repositories import bitacora

empresa = table('empresa', column('idemp', Integer), column('nombre', String))
sucursal = table('sucursal', column('idemp', Integer), column('estado', String))


def _plan(db, plan_id, lock=False):
    query = select(Plan).where(Plan.idplan == plan_id)
    if lock:
        query = query.with_for_update()
    result = db.scalar(query)
    if result is None:
        raise DomainError(404, 'plan_no_encontrado', 'El plan no existe')
    return result


def _codes(db, plan_id):
    return list(db.scalars(select(PlanFuncionalidad.codigo).where(
        PlanFuncionalidad.idplan == plan_id).order_by(PlanFuncionalidad.codigo)))


def _validate_codes(db, values):
    found = set(db.scalars(select(FuncionalidadSaas.codigo).where(FuncionalidadSaas.codigo.in_(values))))
    if found != set(values):
        raise DomainError(422, 'funcionalidad_invalida', 'Una o más funcionalidades no existen')


def _view(db, plan):
    return {'idPlan': plan.idplan, 'nombre': plan.nombre, 'maxSucursales': plan.maxsucursales,
            'estado': plan.estado, 'funcionalidades': _codes(db, plan.idplan)}


def list_plans(db):
    return [_view(db, plan) for plan in db.scalars(select(Plan).order_by(Plan.idplan))]


def list_features(db):
    return [{'codigo': row.codigo, 'descripcion': row.descripcion}
            for row in db.scalars(select(FuncionalidadSaas).order_by(FuncionalidadSaas.codigo))]


def subscriptions(db, plan_id, lock=False):
    _plan(db, plan_id)
    today = datetime.now(ZoneInfo('America/La_Paz')).date()
    branch_count = select(func.count()).select_from(sucursal).where(
        sucursal.c.idemp == SuscripcionPlan.idemp, sucursal.c.estado == 'activo',
    ).correlate(SuscripcionPlan).scalar_subquery()
    active = select(SuscripcionPlan, SuscripcionVersion, PlanVersion, empresa.c.nombre, branch_count).select_from(
        SuscripcionPlan).outerjoin(SuscripcionVersion, SuscripcionVersion.idsusc == SuscripcionPlan.idsusc).outerjoin(
        PlanVersion, PlanVersion.idversion == SuscripcionVersion.idversion).outerjoin(
        empresa, empresa.c.idemp == SuscripcionPlan.idemp).where(
        SuscripcionPlan.idplan == plan_id, SuscripcionPlan.estado == 'activa',
        SuscripcionPlan.fechainicio <= today, SuscripcionPlan.fechavencimiento >= today,
    ).order_by(SuscripcionPlan.idsusc)
    if lock:
        active = active.with_for_update(of=SuscripcionPlan)
    rows = list(db.execute(active))
    if lock and db.get_bind().dialect.name == 'postgresql':
        for idemp in sorted({sub.idemp for sub, *_ in rows}):
            db.execute(text('SELECT pg_advisory_xact_lock(1001, :idemp)'), {'idemp': idemp})
    result = []
    for sub, mapping, version, company, branches in rows:
        if mapping is None or version is None or mapping.idplan != plan_id or version.idplan != plan_id:
            raise DomainError(409, 'version_suscripcion_no_disponible', 'Falta la versión de una suscripción; revise la configuración de la plataforma')
        if company is None:
            raise DomainError(409, 'empresa_no_disponible', 'Una suscripción no tiene empresa válida')
        if lock and db.get_bind().dialect.name == 'postgresql':
            branches = db.scalar(select(func.count()).select_from(sucursal).where(
                sucursal.c.idemp == sub.idemp, sucursal.c.estado == 'activo'))
        result.append({'idSuscripcion': sub.idsusc, 'idEmpresa': sub.idemp, 'empresa': company,
                       'version': version.numero, 'maxSucursales': version.maxsucursales,
                       'sucursalesActivas': branches})
    return result


def preview(db, plan_id, data, lock=False):
    plan = _plan(db, plan_id, lock=lock)
    if plan.estado != 'activo':
        raise DomainError(409, 'plan_inactivo', 'No se puede modificar un plan inactivo')
    _validate_codes(db, data.funcionalidades)
    current = _view(db, plan)
    affected = subscriptions(db, plan_id, lock=lock)
    selected = set(data.suscripcionesSeleccionadas)
    if not selected.issubset({row['idSuscripcion'] for row in affected}):
        raise DomainError(409, 'suscripcion_invalida', 'Una suscripción seleccionada ya no está vigente en este plan')
    selected_companies = {row['idEmpresa'] for row in affected if row['idSuscripcion'] in selected}
    if any(row['idEmpresa'] in selected_companies and row['idSuscripcion'] not in selected for row in affected):
        raise DomainError(409, 'seleccion_empresa_incompleta', 'Debe aplicar la misma selección a las suscripciones vigentes de cada empresa')
    if any(row['sucursalesActivas'] > data.maxSucursales for row in affected if row['idSuscripcion'] in selected):
        raise DomainError(409, 'limite_incompatible', 'Una empresa seleccionada tiene más sucursales activas que el nuevo límite')
    changes = data.model_dump(exclude={'confirmacionImpacto'})
    snapshot = {'actual': current, 'propuesto': changes, 'suscripciones': affected}
    token = hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    return {'actual': current, 'suscripciones': affected, 'seleccionadas': len(selected),
            'conservanVersion': len(affected) - len(selected), 'confirmacionImpacto': token}


def _audit(db, action, user_id, peer, details):
    try:
        ip = str(ip_address(peer)) if peer else None
    except ValueError:
        ip = None
    bitacora.add(db, action=action, user_id=user_id, ip=ip, details=details)


def create(db, data, user_id, peer):
    _validate_codes(db, data.funcionalidades)
    with transaction(db):
        plan = Plan(nombre=data.nombre, maxsucursales=data.maxSucursales, estado=data.estado)
        db.add(plan)
        db.flush()
        db.add_all([PlanFuncionalidad(idplan=plan.idplan, codigo=code) for code in data.funcionalidades])
        version = PlanVersion(idplan=plan.idplan, numero=1, nombre=data.nombre,
                              maxsucursales=data.maxSucursales, estado=data.estado, vigente=True)
        db.add(version)
        db.flush()
        db.add_all([PlanVersionFuncionalidad(idversion=version.idversion, codigo=code) for code in data.funcionalidades])
        _audit(db, 'plan_saas_creado', user_id, peer, {'plan': plan.idplan, 'version': 1})
    return _view(db, plan)


def update(db, plan_id, data, user_id, peer):
    with transaction(db):
        impact = preview(db, plan_id, data, lock=True)
        if data.confirmacionImpacto != impact['confirmacionImpacto']:
            raise DomainError(409, 'confirmacion_requerida', 'Revise el impacto y confirme los cambios con información actualizada')
        plan = _plan(db, plan_id)
        previous = db.scalar(select(PlanVersion).where(PlanVersion.idplan == plan_id, PlanVersion.vigente.is_(True)).with_for_update())
        if previous is None:
            raise DomainError(409, 'version_no_disponible', 'El plan no tiene una versión vigente')
        previous.vigente = False
        db.flush()
        plan.nombre, plan.maxsucursales, plan.estado = data.nombre, data.maxSucursales, data.estado
        db.execute(delete(PlanFuncionalidad).where(PlanFuncionalidad.idplan == plan_id))
        db.add_all([PlanFuncionalidad(idplan=plan_id, codigo=code) for code in data.funcionalidades])
        version = PlanVersion(idplan=plan_id, numero=previous.numero + 1, nombre=data.nombre,
                              maxsucursales=data.maxSucursales, estado=data.estado, vigente=True)
        db.add(version)
        db.flush()
        db.add_all([PlanVersionFuncionalidad(idversion=version.idversion, codigo=code) for code in data.funcionalidades])
        for sub_id in data.suscripcionesSeleccionadas:
            db.get(SuscripcionVersion, sub_id).idversion = version.idversion
        _audit(db, 'plan_saas_actualizado', user_id, peer,
               {'plan': plan_id, 'versionAnterior': previous.numero, 'versionNueva': version.numero,
                'suscripcionesActualizadas': data.suscripcionesSeleccionadas,
                'suscripcionesConVersionAnterior': impact['conservanVersion']})
    return _view(db, plan)


def subscription_version(db, subscription_id):
    """Configuración efectiva que deben consultar las validaciones de suscripción."""
    sub = db.get(SuscripcionPlan, subscription_id)
    mapping = db.get(SuscripcionVersion, subscription_id)
    version = db.get(PlanVersion, mapping.idversion) if mapping else None
    if not sub or not mapping or not version or sub.idplan != mapping.idplan or version.idplan != sub.idplan:
        raise DomainError(409, 'version_suscripcion_no_disponible', 'La suscripción no tiene una versión válida')
    codes = list(db.scalars(select(PlanVersionFuncionalidad.codigo).where(
        PlanVersionFuncionalidad.idversion == version.idversion).order_by(PlanVersionFuncionalidad.codigo)))
    return {'idPlan': version.idplan, 'version': version.numero, 'nombre': version.nombre,
            'maxSucursales': version.maxsucursales, 'estado': version.estado, 'funcionalidades': codes}


def assign_current_version(db, subscription):
    """Vincula una suscripción nueva a la versión actual dentro de su transacción."""
    version = db.scalar(select(PlanVersion).where(
        PlanVersion.idplan == subscription.idplan, PlanVersion.vigente.is_(True)).with_for_update())
    if version is None or version.estado != 'activo':
        raise DomainError(409, 'plan_no_disponible', 'El plan no tiene una versión activa disponible')
    db.add(SuscripcionVersion(idsusc=subscription.idsusc, idplan=subscription.idplan, idversion=version.idversion))
