"""Pruebas de la capa de aplicación; SQLite no reemplaza las pruebas RLS."""
from datetime import date, time, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.access_tokens import AccessTokens
from app.core.database import Base, tenant_context
from app.core.security import utcnow
from app.main import create_app
from app.core.errors import DomainError
from app.modules.seguridad_accesos.models import Funcion, Rol, RolFuncion, Usuario
from app.modules.seguridad_accesos.repositories.usuario import by_email
from app.modules.seguridad_accesos.shared.models import Ciudad, Sucursal
from app.modules.seguridad_accesos.shared.repositories import organizacion as repository
from app.modules.seguridad_accesos.cu09_sucursales_ciudades.services.organizacion import branch_detail, revalidate_actor
from app.modules.cliente_experiencia_compra.shared.models.comercio import HorarioAtencion, HorarioSuc
from app.modules.cliente_experiencia_compra.shared.horarios import guardar_dias


def test_two_companies_same_email_branches_hours_and_session():
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        with factory.begin() as db:
            db.add(Funcion(id="CU09", descripcion="Organización"))
            for tenant in (1, 2):
                db.add(Rol(nro=f"rol-{tenant}", idemp=tenant, descripcion="Operador"))
                db.add(Usuario(idusuario=f"user-{tenant}", idemp=tenant, ci="123", nombre="Ana",
                    apellidopat="P", apellidomat="M", sexo="F", correo="igual@example.com",
                    telefono="0", direccion="Calle", contrasena="hash", fechanac=date(2000, 1, 1),
                    tipo="A", nrorol=f"rol-{tenant}"))
                db.add(Ciudad(id=tenant, idemp=tenant, nombre=f"Ciudad {tenant}"))
                db.add(Sucursal(nro=tenant, idemp=tenant, nombre=f"Sucursal {tenant}",
                                direccion="Calle", estado="activo", idciud=tenant))
                db.add(HorarioAtencion(idaten=tenant, idemp=tenant, horaini=time(8), horafin=time(12)))
                db.add(HorarioSuc(idaten=tenant, nrosuc=tenant, idemp=tenant, dias=guardar_dias([1])))
            db.add(RolFuncion(nrorol="rol-1", idfun="CU09", descripcion="Permitido"))
        tokens = AccessTokens()
        token = tokens.issue("user-1", "hash", utcnow() + timedelta(hours=1), 1)
        assert tokens.get(token).idemp == 1
        for tenant in (1, 2):
            with factory() as db:
                db.info["multitenant"] = True
                tenant_context(db, tenant)
                if tenant == 1:
                    revalidate_actor(db, "user-1")
                    try:
                        revalidate_actor(db, "user-2")
                        assert False, "No debe autorizar un actor de otra empresa"
                    except DomainError as exc:
                        assert exc.status == 401
                else:
                    try:
                        revalidate_actor(db, "user-2")
                        assert False, "No debe heredar permisos de otra empresa"
                    except DomainError as exc:
                        assert exc.status == 403
                assert [u.idusuario for u in by_email(db, "igual@example.com")] == [f"user-{tenant}"]
                assert repository.city(db, 3 - tenant) is None
                assert repository.branch(db, 3 - tenant) is None
                branch = repository.branch(db, tenant)
                assert branch_detail(branch, repository.city(db, tenant), db).horarios[0].dias == [1]
                db.commit()
                assert repository.branch(db, 3 - tenant) is None
                db.rollback()
                assert repository.branch(db, tenant) is not None
    finally:
        engine.dispose()


def test_pending_routes_are_closed_and_login_requires_postgresql(settings, factory):
    config = settings.model_copy(update={"multitenant_enabled": True})
    with TestClient(create_app(config, factory), raise_server_exceptions=False) as client:
        client.headers.update({"Origin": "http://localhost:4200", "X-CSRF-Protection": "1"})
        # Modulos no incluidos en la lista explicita de aislamiento verificado.
        for method, path in (("GET", "/api/catalogo"), ("GET", "/api/admin/roles"),
                             ("GET", "/api/admin/usuarios"), ("GET", "/api/admin/clientes"),
                             ("GET", "/api/admin/bitacora"), ("GET", "/api/admin/reportes")):
            response = client.request(method, path)
            assert response.status_code == 503 and response.json()["error"]["code"] == "modulo_pendiente"
        # Las rutas de autenticacion siguen disponibles: no exponen datos de
        # empresa y son necesarias para iniciar sesion. Con cuerpo invalido
        # responden 422, nunca 503 modulo_pendiente.
        for path in ("/api/auth/registro", "/api/auth/recuperar-contrasena",
                     "/api/auth/restablecer-contrasena"):
            response = client.post(path, json={})
            assert response.status_code == 422, response.text
            assert response.json()["error"]["code"] == "datos_invalidos"
        assert client.post("/api/auth/login", json={"empresa": "uno", "correo": "igual@example.com",
                                                       "contrasena": "123"}).status_code == 401


def test_legacy_bootstrap_never_connects_in_multitenant(settings, monkeypatch):
    from app import bootstrap

    monkeypatch.setattr(bootstrap, "Settings", lambda: settings.model_copy(update={"multitenant_enabled": True}))
    def unexpected_connection(_):
        pytest.fail("El bootstrap legado no debe abrir conexiones en modo multitenant")
    monkeypatch.setattr(bootstrap, "create_database", unexpected_connection)
    with pytest.raises(SystemExit, match="proceso SaaS separado"):
        bootstrap.main(["--crear-rol-cliente"])


def test_legacy_mode_without_idemp_columns(settings, registration):
    """La API antigua no necesita columnas tenant en sus tablas vigentes."""
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        for table in ("usuario", "rol", "bitacora", "ciudad", "sucursal",
                      "horario_atencion", "horario_suc"):
            connection.execute(text(f"ALTER TABLE {table} DROP COLUMN idemp"))
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        with factory.begin() as db:
            db.add(Rol(nro="cliente", descripcion="Cliente"))
        with TestClient(create_app(settings, factory), raise_server_exceptions=False) as client:
            client.headers.update({"Origin": "http://localhost:4200", "X-CSRF-Protection": "1"})
            registered = client.post("/api/auth/registro", json=registration)
            assert registered.status_code == 201
            assert client.post("/api/auth/login", json={"correo": registration["correo"],
                                                        "contrasena": registration["contrasena"]}).status_code == 200
            assert client.get("/api/auth/me").status_code == 200
            with factory.begin() as db:
                db.add(Funcion(id="CU09", descripcion="Organización"))
                db.add(Rol(nro="operador", descripcion="Operador"))
                db.flush()
                db.add(RolFuncion(nrorol="operador", idfun="CU09", descripcion="Permitido"))
                db.get(Usuario, registered.json()["idUsuario"]).nrorol = "operador"
            with factory() as db:
                assert db.get(Usuario, registered.json()["idUsuario"]).nrorol == "operador"
                assert db.get(Rol, "operador").estado == "activo"
            assert client.get("/api/auth/me").status_code == 200
            created = client.post("/api/admin/ciudades", json={"nombre": "Ciudad"})
            assert created.status_code == 201, created.text
            assert client.post("/api/admin/sucursales", json={"nombre": "Central",
                "direccion": "Calle", "idCiud": 1}).status_code == 201
            assert client.get("/api/admin/sucursales/1").status_code == 200
    finally:
        engine.dispose()
