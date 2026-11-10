import pytest
from datetime import date
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.core.security import Passwords
from app.main import create_app
import app.main as main_module
from app.modules.saas_suscripciones.planes_funcionalidades.models.planes import (
    FuncionalidadSaas, Plan, PlanFuncionalidad, SuscripcionPlan,
)
from app.modules.seguridad_accesos.models import Bitacora, Funcion, Rol, RolFuncion, Usuario


def settings(**overrides):
    values = {'database_url': 'postgresql+psycopg://local@localhost/fashionstore',
              'cliente_rol_id': 'C', 'environment': 'development', 'multitenant_enabled': True}
    return Settings(_env_file=None, **(values | overrides))


def test_uso_compartido_es_explicito_y_solo_desarrollo():
    assert not settings().platform_use_database_url
    assert settings(platform_use_database_url=True).platform_use_database_url
    with pytest.raises(ValidationError):
        settings(environment='production', platform_use_database_url=True,
                 cookie_secure=True, allowed_origins=['https://example.com'])
    with pytest.raises(ValidationError):
        settings(platform_use_database_url=True,
                 platform_database_url='postgresql+psycopg://other@localhost/fashionstore')


def test_motor_plataforma_usa_url_existente_solo_con_la_opcion(monkeypatch):
    seen = []
    factory = object()

    class Engine:
        def dispose(self):
            pass

    def create_platform(url):
        seen.append(url)
        return Engine(), factory

    monkeypatch.setattr(main_module, 'create_platform_database', create_platform)
    config = settings(platform_use_database_url=True)
    app = create_app(config, session_factory=object())
    with TestClient(app):
        assert app.state.platform_session_factory is factory
    assert seen == [config.database_url.get_secret_value()]


def test_login_saas_y_consulta_de_planes_con_conexion_compartida(monkeypatch):
    engine = create_engine('sqlite+pysqlite:///:memory:',
                           connect_args={'check_same_thread': False}, poolclass=StaticPool)
    for model in (Rol, Funcion, RolFuncion, Usuario, Bitacora, Plan, FuncionalidadSaas,
                  PlanFuncionalidad, SuscripcionPlan):
        model.__table__.create(engine)
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE empresa (idemp INTEGER PRIMARY KEY, nombre VARCHAR(100) NOT NULL)'))
        conn.execute(text('CREATE TABLE sucursal (idemp INTEGER NOT NULL, estado VARCHAR(20) NOT NULL)'))
    factory = sessionmaker(engine, expire_on_commit=False)
    password = 'Frase de prueba segura 123!'
    with factory.begin() as db:
        db.add_all([Rol(nro='SAAS', descripcion='Administrador SaaS', idemp=None),
                    Funcion(id='SAAS-PLANES', descripcion='Planes')])
        db.flush()
        db.add(RolFuncion(nrorol='SAAS', idfun='SAAS-PLANES', descripcion='Planes', idemp=None))
        db.add(Usuario(idusuario='s1', ci='1', nombre='Admin', apellidopat='Prueba',
                       apellidomat='Prueba', sexo='M', correo='saas@example.com', telefono='1',
                       direccion='Prueba', contrasena=Passwords('argon2id').hash(password),
                       fechanac=date(1990, 1, 1), tipo='S', nrorol='SAAS', idemp=None))
        db.add(Plan(idplan=1, nombre='Básico', maxsucursales=2, estado='activo'))
    monkeypatch.setattr(main_module, 'create_platform_database', lambda _url: (None, factory))
    config = settings(platform_use_database_url=True, multitenant_modules='saas')
    app = create_app(config, session_factory=factory)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            client.headers.update({'Origin': 'http://localhost:4200', 'X-CSRF-Protection': '1'})
            login = client.post('/api/auth/login/saas', json={'correo': 'saas@example.com', 'contrasena': password})
            assert login.status_code == 200
            assert login.json()['plataforma'] is True
            assert login.json()['permisos'] == ['SAAS-PLANES']
            assert client.get('/api/saas/planes').json()[0]['nombre'] == 'Básico'
            assert client.get('/api/saas/empresas').status_code == 403
    finally:
        engine.dispose()
