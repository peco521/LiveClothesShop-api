import hashlib
import json
from ipaddress import ip_address

from sqlalchemy import Integer, String, column, delete, func, select, table, text

from app.core.errors import DomainError
from app.modules.inventario_productos.shared.access import transaction
from app.modules.saas_suscripciones.planes_funcionalidades.models.planes import (
    FuncionalidadSaas, Plan, PlanFuncionalidad, SuscripcionPlan,
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
    branch_count = select(func.count()).select_from(sucursal).where(
        sucursal.c.idemp == SuscripcionPlan.idemp, sucursal.c.estado == 'activo',
    ).correlate(SuscripcionPlan).scalar_subquery()
    active = select(SuscripcionPlan, empresa.c.nombre, branch_count).select_from(
        SuscripcionPlan).outerjoin(empresa, empresa.c.idemp == SuscripcionPlan.idemp).where(
        SuscripcionPlan.idplan == plan_id, SuscripcionPlan.estado == 'activa',
    ).order_by(SuscripcionPlan.idsusc)
    if lock:
        active = active.with_for_update(of=SuscripcionPlan)
    rows = list(db.execute(active))
    if lock and db.get_bind().dialect.name == 'postgresql':
        for idemp in sorted({sub.idemp for sub, *_ in rows}):
            db.execute(text('SELECT pg_advisory_xact_lock(1001, :idemp)'), {'idemp': idemp})
    result = []
    plan = _plan(db, plan_id)
    for sub, company, branches in rows:
        if company is None:
            raise DomainError(409, 'empresa_no_disponible', 'Una suscripción no tiene empresa válida')
        if lock and db.get_bind().dialect.name == 'postgresql':
            branches = db.scalar(select(func.count()).select_from(sucursal).where(
                sucursal.c.idemp == sub.idemp, sucursal.c.estado == 'activo'))
        result.append({'idSuscripcion': sub.idsusc, 'idEmpresa': sub.idemp, 'empresa': company,
                       'maxSucursales': plan.maxsucursales,
                       'sucursalesActivas': branches})
    return result


def preview(db, plan_id, data, lock=False):
    plan = _plan(db, plan_id, lock=lock)
    if plan.estado != 'activo':
        raise DomainError(409, 'plan_inactivo', 'No se puede modificar un plan inactivo')
    _validate_codes(db, data.funcionalidades)
    current = _view(db, plan)
    affected = subscriptions(db, plan_id, lock=lock)
    if any(row['sucursalesActivas'] > data.maxSucursales for row in affected):
        raise DomainError(409, 'limite_incompatible', 'Una empresa tiene más sucursales activas que el nuevo límite')
    references_query = select(SuscripcionPlan).where(SuscripcionPlan.idplan == plan_id).order_by(SuscripcionPlan.idsusc)
    if lock:
        references_query = references_query.with_for_update()
    references = [{'id': item.idsusc, 'empresa': item.idemp, 'estado': item.estado,
                   'inicio': item.fechainicio.isoformat(), 'fin': item.fechavencimiento.isoformat()}
                  for item in db.scalars(references_query)]
    changes = data.model_dump(exclude={'confirmacionImpacto'})
    snapshot = {'actual': current, 'propuesto': changes, 'suscripciones': affected, 'referencias': references}
    token = hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    return {'actual': current, 'suscripciones': affected, 'suscripcionesActivas': len(affected),
            'empresasActivas': len({item['idEmpresa'] for item in affected}),
            'suscripcionesAsociadas': len(references), 'confirmacionImpacto': token}


def _audit(db, action, user_id, peer, details):
    try:
        ip = str(ip_address(peer)) if peer else None
    except ValueError:
        ip = None
    bitacora.add(db, action=action, user_id=user_id, ip=ip, details=details)


def _sync_plan_version(db, plan_id: int, nombre: str, max_sucursales: int, estado: str, funcionalidades: list[str]) -> None:
    if db.get_bind().dialect.name != 'postgresql':
        return
    if not db.scalar(text("SELECT to_regclass('public.plan_version') IS NOT NULL")):
        return
    version_id = db.scalar(text(
        "SELECT idversion FROM plan_version WHERE idplan = :idplan AND vigente ORDER BY idversion DESC LIMIT 1"
    ), {'idplan': plan_id})
    if version_id is None:
        next_num = db.scalar(text(
            "SELECT coalesce(max(numero), 0) + 1 FROM plan_version WHERE idplan = :idplan"
        ), {'idplan': plan_id}) or 1
        version_id = db.scalar(text("""
            INSERT INTO plan_version(idplan, numero, nombre, maxsucursales, estado, vigente)
            VALUES (:idplan, :numero, :nombre, :maxsucursales, :estado, true)
            RETURNING idversion
        """), {'idplan': plan_id, 'numero': next_num, 'nombre': nombre,
               'maxsucursales': max_sucursales, 'estado': estado})
    else:
        db.execute(text("""
            UPDATE plan_version
            SET nombre = :nombre, maxsucursales = :maxsucursales, estado = :estado
            WHERE idversion = :idversion
        """), {'idversion': version_id, 'nombre': nombre,
               'maxsucursales': max_sucursales, 'estado': estado})
    if db.scalar(text("SELECT to_regclass('public.plan_version_funcionalidad') IS NOT NULL")):
        db.execute(text("DELETE FROM plan_version_funcionalidad WHERE idversion = :idversion"),
                   {'idversion': version_id})
        for code in funcionalidades:
            db.execute(text("""
                INSERT INTO plan_version_funcionalidad(idversion, codigo)
                VALUES (:idversion, :codigo)
                ON CONFLICT DO NOTHING
            """), {'idversion': version_id, 'codigo': code})


def create(db, data, user_id, peer):
    _validate_codes(db, data.funcionalidades)
    with transaction(db):
        plan = Plan(nombre=data.nombre, maxsucursales=data.maxSucursales, estado=data.estado)
        db.add(plan)
        db.flush()
        db.add_all([PlanFuncionalidad(idplan=plan.idplan, codigo=code) for code in data.funcionalidades])
        _sync_plan_version(db, plan.idplan, data.nombre, data.maxSucursales, data.estado, data.funcionalidades)
        _audit(db, 'plan_saas_creado', user_id, peer, {'plan': plan.idplan})
    return _view(db, plan)


def update(db, plan_id, data, user_id, peer):
    with transaction(db):
        impact = preview(db, plan_id, data, lock=True)
        if data.confirmacionImpacto != impact['confirmacionImpacto']:
            raise DomainError(409, 'confirmacion_requerida', 'Revise el impacto y confirme los cambios con información actualizada')
        plan = _plan(db, plan_id)
        previous = _view(db, plan)
        plan.nombre, plan.maxsucursales, plan.estado = data.nombre, data.maxSucursales, data.estado
        db.execute(delete(PlanFuncionalidad).where(PlanFuncionalidad.idplan == plan_id))
        db.add_all([PlanFuncionalidad(idplan=plan_id, codigo=code) for code in data.funcionalidades])
        _sync_plan_version(db, plan_id, data.nombre, data.maxSucursales, data.estado, data.funcionalidades)
        _audit(db, 'plan_saas_actualizado', user_id, peer,
               {'plan': plan_id, 'anterior': previous, 'nuevo': data.model_dump(exclude={'confirmacionImpacto'}),
                'suscripcionesActivas': impact['suscripcionesActivas'],
                'suscripcionesAsociadas': impact['suscripcionesAsociadas']})
    return _view(db, plan)


def subscription_version(db, subscription_id):
    """Configuración del plan asociado a la suscripción en el esquema existente."""
    sub = db.get(SuscripcionPlan, subscription_id)
    if sub is None:
        raise DomainError(404, 'suscripcion_no_encontrada', 'La suscripción no existe')
    return _view(db, _plan(db, sub.idplan))
