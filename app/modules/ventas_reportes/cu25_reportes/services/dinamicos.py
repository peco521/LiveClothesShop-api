"""Tenant-fixed analytical reads and immutable, owner-bound export snapshots."""
from collections import OrderedDict
from copy import deepcopy
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from threading import RLock
from time import monotonic
from uuid import uuid4
from zoneinfo import ZoneInfo
from sqlalchemy import text
from app.core.errors import DomainError
from app.modules.seguridad_accesos.repositories import bitacora
from ..schemas.dinamicos import ReportPlan

TITLES = {'ventas_sucursal': 'Ventas por sucursal y período',
          'productos_vendidos': 'Productos más vendidos', 'ventas_semana': 'Evolución semanal de ventas',
          'existencias_bajas': 'Productos con menor existencia por sucursal'}
GROUPS = {'ventas_sucursal': 'sucursal', 'productos_vendidos': 'producto',
          'ventas_semana': 'semana', 'existencias_bajas': 'sucursal y variante'}
_snapshots = OrderedDict()
_lock = RLock()
TTL = 900

def tenant(db):
    value = db.info.get('idemp')
    if db.info.get('multitenant') is not True or not isinstance(value, int) or value < 1:
        raise DomainError(403, 'contexto_empresa_requerido', 'Se requiere una sesión empresarial')
    return value

def feature(db, code='REPORTES'):
    allowed = db.scalar(text('SELECT fn_tiene_funcionalidad(CAST(:tenant AS integer), CAST(:code AS varchar))'),
                        {'tenant': tenant(db), 'code': code})
    if allowed is not True:
        raise DomainError(403, 'funcionalidad_no_habilitada', 'El plan vigente no incluye esta funcionalidad')

def actor_scope(db, identity):
    company = tenant(db)
    if identity.usuario.tipo == 'A':
        return (identity.usuario.idUsuario, None)
    if identity.usuario.tipo == 'E':
        branch = db.scalar(text("SELECT nrosuc FROM empleado WHERE idemp=:tenant AND idusuario=:user AND estado='activo'"),
                           {'tenant': company, 'user': identity.usuario.idUsuario})
        if branch is not None:
            return (identity.usuario.idUsuario, branch)
    raise DomainError(403, 'acceso_denegado', 'Se requiere administrador o empleado autorizado')

def references(db, actor):
    feature(db)
    return [dict(row) for row in db.execute(text("SELECT nro,nombre FROM sucursal WHERE idemp=:tenant "
                 "AND (CAST(:scope AS integer) IS NULL OR nro=:scope) AND estado='activo' ORDER BY nombre"),
                 {'tenant': tenant(db), 'scope': actor[1]}).mappings()]

def _scope(db, plan, actor):
    company = tenant(db)
    if actor[1] is not None and plan.nroSuc not in (None, actor[1]):
        raise DomainError(404, 'sucursal_no_encontrada', 'Sucursal no disponible')
    branch = actor[1] if actor[1] is not None else plan.nroSuc
    if branch is not None and db.scalar(text('SELECT nro FROM sucursal WHERE idemp=:tenant AND nro=:branch'),
                                        {'tenant': company, 'branch': branch}) is None:
        raise DomainError(404, 'sucursal_no_encontrada', 'Sucursal no disponible')
    return branch

# Every joined table carries the tenant, independently of RLS. EXISTS avoids
# multiplying line amounts when a sale has more than one approved payment.
SALES = """FROM venta v JOIN detalleventa d ON d.idemp=v.idemp AND d.nroventa=v.nroventa
    JOIN sucursal s ON s.idemp=v.idemp AND s.nro=v.nrosuc
    JOIN varianteprod vp ON vp.idemp=d.idemp AND vp.idvariante=d.idvar
    JOIN producto p ON p.idemp=vp.idemp AND p.idprod=vp.idprod
    WHERE v.idemp=:tenant AND v.estado='registrada'
      AND v.fechahora >= :start AND v.fechahora < :end
      AND (CAST(:branch AS integer) IS NULL OR v.nrosuc=:branch)
      AND EXISTS (SELECT 1 FROM pago pg WHERE pg.idemp=v.idemp AND pg.nroventa=v.nroventa AND pg.estado='aprobado')"""
AMOUNT = 'sum((d.preciounitario-coalesce(d.descuentounitario,0))*d.cantidad)'

def calculate(db, plan: ReportPlan, actor, zone='America/La_Paz', *, check_feature=True,
              audit=True, source_label=None, keep_snapshot=True):
    if check_feature:
        feature(db)
    branch = _scope(db, plan, actor)
    params = {'tenant': tenant(db), 'branch': branch, 'limit': plan.limite, 'threshold': plan.umbral, 'zone': zone}
    if plan.fechaIni:
        tz = ZoneInfo(zone)
        params['start'] = datetime.combine(plan.fechaIni, time.min, tz).astimezone(timezone.utc).replace(tzinfo=None)
        params['end'] = datetime.combine(plan.fechaFin+timedelta(days=1), time.min, tz).astimezone(timezone.utc).replace(tzinfo=None)
    if plan.tipo == 'ventas_sucursal':
        query = f"SELECT s.nro AS sucursal_id,s.nombre AS sucursal,count(DISTINCT v.nroventa) AS ventas,sum(d.cantidad) AS unidades,{AMOUNT} AS importe " + SALES + ' GROUP BY s.nro,s.nombre ORDER BY s.nro LIMIT :limit'
        columns = ['sucursal_id','sucursal','ventas','unidades','importe']
    elif plan.tipo == 'productos_vendidos':
        query = f'SELECT p.idprod AS producto_id,p.descripcion AS producto,sum(d.cantidad) AS unidades,{AMOUNT} AS importe ' + SALES + ' GROUP BY p.idprod,p.descripcion ORDER BY unidades DESC,p.idprod LIMIT :limit'
        columns = ['producto_id','producto','unidades','importe']
    elif plan.tipo == 'ventas_semana':
        # Timestamp storage is UTC-naive; business weeks are local Monday starts.
        week = "to_char(date_trunc('week', v.fechahora AT TIME ZONE 'UTC' AT TIME ZONE :zone),'YYYY-MM-DD')"
        query = f'SELECT {week} AS semana,count(DISTINCT v.nroventa) AS ventas,sum(d.cantidad) AS unidades,{AMOUNT} AS importe ' + SALES + f' GROUP BY {week} ORDER BY semana LIMIT :limit'
        columns = ['semana','ventas','unidades','importe']
    else:
        query = """SELECT s.nro AS sucursal_id,s.nombre AS sucursal,p.descripcion AS producto,vp.idvariante AS variante,
                    i.stock AS stock_fisico,i.cantdisp AS disponible
            FROM inventario i JOIN sucursal s ON s.idemp=i.idemp AND s.nro=i.nrosuc
            JOIN varianteprod vp ON vp.idemp=i.idemp AND vp.idvariante=i.idvar
            JOIN producto p ON p.idemp=vp.idemp AND p.idprod=vp.idprod
            WHERE i.idemp=:tenant AND (CAST(:branch AS integer) IS NULL OR i.nrosuc=:branch) AND i.cantdisp<=:threshold
            ORDER BY i.cantdisp,s.nro,vp.idvariante LIMIT :limit"""
        columns = ['sucursal_id','sucursal','producto','variante','stock_fisico','disponible']
    rows = [dict(row) for row in db.execute(text(query), params).mappings()]
    for row in rows:
        for key, value in row.items():
            if isinstance(value, Decimal): row[key] = str(value.quantize(Decimal('0.01')))
    result = {'titulo': TITLES[plan.tipo], 'plan': plan.model_dump(mode='json'), 'agrupacion': GROUPS[plan.tipo],
              'columnas': columns, 'filas': rows, 'generadoEn': datetime.now(timezone.utc).isoformat(),
              'procedencia': source_label or 'PostgreSQL: datos comerciales reales de la empresa autenticada',
              'nota': 'Inventario conocido al consultar; el período no filtra existencias.' if plan.tipo=='existencias_bajas' else 'Ventas registradas con pago aprobado; importes de líneas netos de descuento. Fechas de negocio: '+zone}
    if audit:
        bitacora.add(db, action='reporte_dinamico_consultado', user_id=actor[0], ip=None,
                     details={'resultado':'exito','tipo':plan.tipo,'filas':len(rows)})
        db.commit()
    if not keep_snapshot:
        return result
    token = str(uuid4())
    with _lock:
        _prune()
        _snapshots[token] = (monotonic()+TTL, tenant(db), actor, deepcopy(result))
    return {'resultadoId':token, **result}

def _prune():
    for key in list(_snapshots):
        if _snapshots[key][0] <= monotonic(): del _snapshots[key]
    while len(_snapshots) >= 256: _snapshots.popitem(last=False)

def snapshot(db, result_id, actor):
    feature(db)
    with _lock:
        _prune()
        found = _snapshots.get(str(result_id))
        if found is None or found[1]!=tenant(db) or found[2]!=actor:
            raise DomainError(404, 'resultado_no_disponible', 'Vuelve a calcular el reporte (resultado vencido o no disponible)')
        return deepcopy(found[3])

def export_model(result):
    # Reuse the established PDF/XLSX renderers, not their legacy unscoped query.
    numeric = {'ventas','unidades','stock_fisico','disponible','sucursal_id'}
    return {'titulo': result['titulo'], 'filtros':[('Generado:',result['generadoEn']),('Origen:',result['procedencia'])],
            'columnas':[(key.replace('_',' ').title(),'moneda' if key=='importe' else 'entero' if key in numeric else 'texto',24)
                        for key in result['columnas']],
            'filas':[[Decimal(row[key]) if key=='importe' else row[key] for key in result['columnas']] for row in result['filas']],
            'total':None,'hojas':[], 'nota':result['nota']}
