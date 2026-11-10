from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.database import get_platform_db
from app.core.errors import DomainError, register_handlers
from app.modules.saas_suscripciones.shared.dependencies import platform_identity
from app.modules.saas_suscripciones.planes_funcionalidades.routers.planes import router
from app.modules.saas_suscripciones.planes_funcionalidades.tests.test_esquema_real import db


@pytest.fixture
def api(db):
    app = FastAPI()
    register_handlers(app)
    app.include_router(router)
    identity = {'value': None}

    def current_identity():
        if identity['value'] is None:
            raise DomainError(401, 'autenticacion_rechazada', 'Inicia sesión')
        return identity['value']

    app.dependency_overrides[platform_identity] = current_identity
    app.dependency_overrides[get_platform_db] = lambda: db
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, identity


def test_api_exige_sesion_y_permiso_de_planes(api):
    client, identity = api
    assert client.get('/api/saas/planes').status_code == 401
    identity['value'] = SimpleNamespace(usuario=SimpleNamespace(idUsuario='empresa'), permisos=[])
    assert client.get('/api/saas/planes').status_code == 403
    identity['value'] = SimpleNamespace(usuario=SimpleNamespace(idUsuario='saas'), permisos=['SAAS-PLANES'])
    assert client.get('/api/saas/planes').status_code == 200


def test_api_crea_y_actualiza_plan_global_con_confirmacion(api):
    client, identity = api
    identity['value'] = SimpleNamespace(usuario=SimpleNamespace(idUsuario='saas'), permisos=['SAAS-PLANES'])
    created = client.post('/api/saas/planes', json={
        'nombre': 'Básico', 'maxSucursales': 2, 'estado': 'activo', 'funcionalidades': ['VENTAS'],
    })
    assert created.status_code == 201
    plan_id = created.json()['idPlan']
    data = {'nombre': 'Básico', 'maxSucursales': 3, 'estado': 'activo', 'funcionalidades': ['VENTAS']}
    assert client.put(f'/api/saas/planes/{plan_id}', json=data).status_code == 409
    preview = client.post(f'/api/saas/planes/{plan_id}/impacto', json=data)
    assert preview.status_code == 200
    assert preview.json()['suscripcionesAsociadas'] == 0
    data['confirmacionImpacto'] = preview.json()['confirmacionImpacto']
    updated = client.put(f'/api/saas/planes/{plan_id}', json=data)
    assert updated.status_code == 200
    assert updated.json()['maxSucursales'] == 3
