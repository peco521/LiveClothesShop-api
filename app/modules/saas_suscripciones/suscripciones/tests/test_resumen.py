from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.database import get_platform_db
from app.core.errors import register_handlers
from app.modules.saas_suscripciones.suscripciones.services import plataforma as services
from app.modules.saas_suscripciones.shared.dependencies import platform_identity
from app.modules.saas_suscripciones.suscripciones.routers.plataforma import router


@pytest.fixture
def db():
    engine = create_engine('sqlite://', poolclass=StaticPool, connect_args={'check_same_thread': False})
    with engine.begin() as c:
        for sql in [
            'CREATE TABLE empresa (idemp INTEGER PRIMARY KEY,codigo TEXT,nombre TEXT,estado TEXT)',
            'CREATE TABLE plan (idplan INTEGER PRIMARY KEY,nombre TEXT,maxsucursales INTEGER)',
            'CREATE TABLE suscripcion (idemp INTEGER,idplan INTEGER,estado TEXT,fechainicio DATE,fechavencimiento DATE)',
            'CREATE TABLE sucursal (idemp INTEGER,estado TEXT)',
            "INSERT INTO empresa VALUES (1,'uno','Uno','activa'),(2,'dos','Dos','suspendida'),(3,'tres','Tres','inactiva')",
            "INSERT INTO plan VALUES (1,'Basico',2),(2,'Profesional',10)",
            "INSERT INTO sucursal VALUES (1,'activo'),(1,'activo'),(2,'inactivo')",
            "INSERT INTO suscripcion VALUES (1,1,'activa','2026-01-01','2026-11-09'),(2,2,'activa','2026-01-01','2027-01-01'),(3,1,'activa','2026-01-01','2026-11-08'),(3,1,'vencida','2025-01-01','2025-02-01'),(3,1,'cancelada','2025-01-01','2025-02-01'),(3,1,'activa','2026-12-01','2027-01-01')",
        ]:
            c.execute(text(sql))
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_resumen_cuenta_fechas_estados_y_no_exige_columnas_ajenas(db):
    result = services.resumen_plataforma(db, hoy=date(2026, 11, 9))
    assert result['empresas'] == {'total': 3, 'activas': 1, 'suspendidas': 1, 'inactivas': 1}
    assert result['suscripciones'] == {'vigentes': 2, 'vencidas': 2, 'proximasAVencer': 1}
    assert result['sucursalesActivas'] == 2
    assert result['distribucionPlanes'] == [{'nombre': 'Basico', 'suscripciones': 1}, {'nombre': 'Profesional', 'suscripciones': 1}]
    assert result['actividadReciente'] == []
    assert services.listar_empresas(db)[0]['nombre'] == 'Uno'


def test_endpoint_exige_permiso_y_no_expone_auditoria(db):
    app = FastAPI()
    register_handlers(app)
    app.include_router(router)
    permissions = ['SAAS-PLANES']
    app.dependency_overrides[get_platform_db] = lambda: db
    app.dependency_overrides[platform_identity] = lambda: SimpleNamespace(permisos=permissions)
    with TestClient(app) as client:
        assert client.get('/api/saas/resumen').status_code == 403
        permissions.append('SAAS-EMPRESAS')
        response = client.get('/api/saas/resumen')
        assert response.status_code == 200
        assert response.json()['empresas']['total'] == 3
        assert response.json()['actividadReciente'] == []
        assert client.get('/api/saas/empresas').status_code == 200
