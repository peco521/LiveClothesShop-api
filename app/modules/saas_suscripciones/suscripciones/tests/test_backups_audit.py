from pathlib import Path
from types import SimpleNamespace
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import get_platform_db
from app.core.errors import DomainError, register_handlers
from app.modules.saas_suscripciones.shared.backups import BackupManager
from app.modules.saas_suscripciones.shared.dependencies import platform_identity
from app.modules.saas_suscripciones.suscripciones.routers.plataforma import router
from app.modules.seguridad_accesos.models import Bitacora


@pytest.fixture
def manager(tmp_path, monkeypatch):
    dump = tmp_path/'pg_dump.exe'; dump.touch()
    restore = tmp_path/'pg_restore.exe'; restore.touch()
    settings = SimpleNamespace(backups_directory=str(tmp_path/'private'), backups_pg_dump=str(dump),
        backups_timeout_seconds=30, platform_database_url=None, platform_use_database_url=True,
        database_url=SecretStr('postgresql+psycopg://test:secret@localhost/test'))
    result = BackupManager(settings)
    result.restore = restore
    def command(args, env):
        assert env['PGOPTIONS'] == '-c default_transaction_read_only=on'
        assert all('secret' not in arg for arg in args)
        for arg in args:
            if arg.startswith('--file='):
                Path(arg.split('=',1)[1]).write_bytes(b'PGDMP-test-data')
    monkeypatch.setattr(result, '_run_command', command)
    return result


def test_respaldo_persistente_bloquea_concurrencia_y_valida_integridad(manager):
    job, lease = manager.prepare()
    with pytest.raises(DomainError) as exc:
        manager.prepare()
    assert exc.value.status == 409
    manager.run(job['id'], lease)
    assert manager.list()['respaldos'][0]['estado'] == 'completado'
    path = manager.download(job['id'])
    path.write_bytes(b'altered')
    with pytest.raises(DomainError) as exc:
        manager.download(job['id'])
    assert exc.value.code == 'respaldo_alterado'
    with pytest.raises(DomainError):
        manager.download('../secrets')


def test_fallo_no_expone_credenciales_y_permite_reintentar(manager, monkeypatch):
    def fail(*args): raise RuntimeError('secret credentials')
    monkeypatch.setattr(manager, '_run_command', fail)
    job, lease = manager.prepare()
    manager.run(job['id'], lease)
    result = manager.list()['respaldos'][0]
    assert result['estado'] == 'fallido' and 'secret' not in str(result)
    job, lease = manager.prepare()
    manager.abort(job, lease)


def test_recupera_solicitud_interrumpida(manager):
    job, lease = manager.prepare()
    lease.close()
    assert manager.list()['respaldos'][0]['estado'] == 'fallido'


@pytest.fixture
def client(manager):
    engine=create_engine('sqlite://',poolclass=StaticPool,connect_args={'check_same_thread':False})
    Bitacora.__table__.create(engine)
    factory=sessionmaker(engine)
    app=FastAPI(); register_handlers(app); app.include_router(router)
    app.state.backups=manager
    permissions=[]
    def session():
        with factory() as db: yield db
    app.dependency_overrides[get_platform_db]=session
    app.dependency_overrides[platform_identity]=lambda: SimpleNamespace(permisos=permissions, usuario=SimpleNamespace(idUsuario='admin'))
    with TestClient(app) as http:
        yield http,permissions,factory
    engine.dispose()


def test_api_respaldos_requiere_permiso_y_audita_creacion_descarga(client):
    http,permissions,factory=client
    assert http.get('/api/saas/respaldos').status_code==403
    assert http.post('/api/saas/respaldos',json={}).status_code==403
    assert http.get('/api/saas/respaldos/'+'0'*32+'/descarga').status_code==403
    permissions.append('SAAS-RESPALDOS')
    response=http.post('/api/saas/respaldos',json={})
    assert response.status_code==202
    job=response.json()
    assert http.get('/api/saas/respaldos').json()['respaldos'][0]['estado']=='completado'
    download=http.get('/api/saas/respaldos/'+job['id']+'/descarga')
    assert download.status_code==200 and download.content.startswith(b'PGDMP')
    with factory() as db:
        assert db.query(Bitacora).count()==2


def test_auditoria_filtra_literal_pagina_y_excluye_empresas(client):
    http,permissions,factory=client
    with factory.begin() as db:
        for action,company in [('login_ok',None),('login_error',None),('login_ok',9),('otro',None)]:
            db.execute(text("INSERT INTO bitacora(accion,idemp,fecha,detalles) VALUES (:accion,:idemp,:fecha,'{}')"),
                       {'accion':action, 'idemp':company, 'fecha':datetime(2026,1,1)})
    assert http.get('/api/saas/auditoria').status_code==403
    permissions.append('SAAS-AUDITORIA')
    first=http.get('/api/saas/auditoria?accion=LOGIN&limite=1').json()
    second=http.get('/api/saas/auditoria?accion=LOGIN&limite=1&desplazamiento=1').json()
    assert first[0]['accion']=='login_error' and second[0]['accion']=='login_ok'
    assert first[0]['id']!=second[0]['id']
    assert http.get('/api/saas/auditoria?accion=%25').json()==[]
    assert http.get('/api/saas/auditoria?desplazamiento=-1').status_code==422
