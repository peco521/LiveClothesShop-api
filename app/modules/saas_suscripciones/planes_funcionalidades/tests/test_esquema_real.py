from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.modules.saas_suscripciones.planes_funcionalidades.models.planes import (
    FuncionalidadSaas, Plan, PlanFuncionalidad, SuscripcionPlan,
)
from app.core.errors import DomainError
from app.core.database import Base
from app.modules.saas_suscripciones.planes_funcionalidades.schemas.planes import PlanActualizacion, PlanDatos
from app.modules.saas_suscripciones.planes_funcionalidades.services import planes
from app.modules.seguridad_accesos.models import Bitacora


@pytest.fixture
def db():
    engine = create_engine('sqlite+pysqlite:///:memory:', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    for model in (Plan, FuncionalidadSaas, PlanFuncionalidad, SuscripcionPlan):
        model.__table__.create(engine)
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE empresa (idemp INTEGER PRIMARY KEY, nombre VARCHAR(100) NOT NULL)'))
        conn.execute(text('CREATE TABLE sucursal (idemp INTEGER NOT NULL, estado VARCHAR(20) NOT NULL)'))
        conn.execute(text('CREATE TABLE bitacora (id INTEGER PRIMARY KEY, usuario_id VARCHAR(100), accion VARCHAR(150) NOT NULL, ip VARCHAR, detalles JSON, fecha DATETIME DEFAULT CURRENT_TIMESTAMP)'))
        conn.execute(text("INSERT INTO empresa VALUES (1, 'Empresa A')"))
        conn.execute(text("INSERT INTO sucursal VALUES (1, 'activo')"))
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        session.add_all([FuncionalidadSaas(codigo='VENTAS', descripcion='Ventas'),
                         FuncionalidadSaas(codigo='RESERVAS', descripcion='Reservas')])
        session.commit()
        yield session
    engine.dispose()


def test_crear_y_consultar_plan_sin_tablas_de_version(db):
    assert not any('version' in name for name in inspect(db.get_bind()).get_table_names())
    assert 'plan_version' not in Base.metadata.tables
    assert 'suscripcion_plan_version' not in Base.metadata.tables
    result = planes.create(db, PlanDatos(nombre='Profesional', maxSucursales=10,
                                        funcionalidades=['VENTAS', 'RESERVAS']), 'admin-saas', None)
    assert result['nombre'] == 'Profesional'
    assert result['funcionalidades'] == ['RESERVAS', 'VENTAS']
    assert planes.list_plans(db) == [result]
    assert db.scalar(select(Bitacora).where(Bitacora.accion == 'plan_saas_creado')) is not None


def test_consultar_suscripciones_con_tablas_existentes(db):
    plan = planes.create(db, PlanDatos(nombre='Básico', maxSucursales=2,
                                      funcionalidades=['VENTAS']), 'admin-saas', None)
    db.add(SuscripcionPlan(idsusc=1, idemp=1, idplan=plan['idPlan'], estado='activa',
                           fechainicio=date.today() - timedelta(days=1),
                           fechavencimiento=date.today() + timedelta(days=30)))
    db.commit()
    assert planes.subscriptions(db, plan['idPlan']) == [
        {'idSuscripcion': 1, 'idEmpresa': 1, 'empresa': 'Empresa A',
         'maxSucursales': 2, 'sucursalesActivas': 1},
    ]


def test_editar_plan_sin_suscripciones_con_confirmacion(db):
    plan = planes.create(db, PlanDatos(nombre='Básico', maxSucursales=2,
                                      funcionalidades=['VENTAS']), 'admin-saas', None)
    data = PlanActualizacion(nombre='Básico', maxSucursales=3, funcionalidades=['RESERVAS'])
    preview = planes.preview(db, plan['idPlan'], data)
    data.confirmacionImpacto = preview['confirmacionImpacto']
    result = planes.update(db, plan['idPlan'], data, 'admin-saas', None)
    assert result['maxSucursales'] == 3
    assert result['funcionalidades'] == ['RESERVAS']
    assert db.scalar(select(Bitacora).where(Bitacora.accion == 'plan_saas_actualizado')) is not None


def test_impacto_de_plan_compartido_incluye_todas_las_suscripciones(db):
    plan = planes.create(db, PlanDatos(nombre='Básico', maxSucursales=2,
                                      funcionalidades=['VENTAS']), 'admin-saas', None)
    db.add(SuscripcionPlan(idsusc=1, idemp=1, idplan=plan['idPlan'], estado='activa',
                           fechainicio=date.today() - timedelta(days=1),
                           fechavencimiento=date.today() + timedelta(days=30)))
    db.add(SuscripcionPlan(idsusc=2, idemp=1, idplan=plan['idPlan'], estado='cancelada',
                           fechainicio=date.today() - timedelta(days=100),
                           fechavencimiento=date.today() - timedelta(days=30)))
    db.commit()
    data = PlanActualizacion(nombre='Básico', maxSucursales=3, funcionalidades=['VENTAS'])
    impact = planes.preview(db, plan['idPlan'], data)
    assert impact['suscripcionesActivas'] == 1
    assert impact['empresasActivas'] == 1
    assert impact['suscripcionesAsociadas'] == 2
    data.confirmacionImpacto = impact['confirmacionImpacto']
    planes.update(db, plan['idPlan'], data, 'admin-saas', None)
    assert planes.subscription_version(db, 1)['maxSucursales'] == 3
    assert planes.subscription_version(db, 2)['maxSucursales'] == 3


def test_rechazar_limite_inferior_a_sucursales_activas(db):
    plan = planes.create(db, PlanDatos(nombre='Básico', maxSucursales=2,
                                      funcionalidades=['VENTAS']), 'admin-saas', None)
    db.add(SuscripcionPlan(idsusc=1, idemp=1, idplan=plan['idPlan'], estado='activa',
                           fechainicio=date.today() - timedelta(days=1),
                           fechavencimiento=date.today() + timedelta(days=30)))
    db.commit()
    db.execute(text("INSERT INTO sucursal VALUES (1, 'activo')"))
    db.commit()
    data = PlanActualizacion(nombre='Básico', maxSucursales=1, funcionalidades=['VENTAS'])
    with pytest.raises(DomainError) as error:
        planes.preview(db, plan['idPlan'], data)
    assert error.value.code == 'limite_incompatible'
    assert planes.subscription_version(db, 1)['maxSucursales'] == 2


def test_confirmacion_invalida_no_modifica_plan(db):
    plan = planes.create(db, PlanDatos(nombre='Básico', maxSucursales=2,
                                      funcionalidades=['VENTAS']), 'admin-saas', None)
    data = PlanActualizacion(nombre='Básico', maxSucursales=3, funcionalidades=['VENTAS'])
    data.confirmacionImpacto = planes.preview(db, plan['idPlan'], data)['confirmacionImpacto']
    data.maxSucursales = 4
    with pytest.raises(DomainError) as error:
        planes.update(db, plan['idPlan'], data, 'admin-saas', None)
    assert error.value.code == 'confirmacion_requerida'
    assert db.get(Plan, plan['idPlan']).maxsucursales == 2


def test_fallo_en_bitacora_revierte_cambio_del_plan(db, monkeypatch):
    plan = planes.create(db, PlanDatos(nombre='Básico', maxSucursales=2,
                                      funcionalidades=['VENTAS']), 'admin-saas', None)
    data = PlanActualizacion(nombre='Básico', maxSucursales=3, funcionalidades=['RESERVAS'])
    data.confirmacionImpacto = planes.preview(db, plan['idPlan'], data)['confirmacionImpacto']

    def fallo(*_args, **_kwargs):
        raise RuntimeError('Fallo de bitácora simulado')

    monkeypatch.setattr(planes, '_audit', fallo)
    with pytest.raises(RuntimeError):
        planes.update(db, plan['idPlan'], data, 'admin-saas', None)
    actual = planes.list_plans(db)[0]
    assert actual['maxSucursales'] == 2
    assert actual['funcionalidades'] == ['VENTAS']
