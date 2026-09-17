"""CU07 HTTP/domain regression: injected SQLite only, no real provisioning."""
from contextlib import closing
import json
from datetime import timedelta
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateTable

from app.core.errors import DomainError
from app.core.security import digest, utcnow
from app.modules.seguridad_accesos.models import (
    Admin, Bitacora, Cliente, Funcion, RecuperacionContrasena, Rol, RolFuncion, Usuario,
)
from app.modules.seguridad_accesos.cu05_usuarios_empleados.models import Empleado
from app.modules.seguridad_accesos.shared.models import Ciudad, Sucursal
from app.modules.seguridad_accesos.shared.repositories import continuidad
from app.modules.seguridad_accesos.cu07_clientes.repositories import cliente as repository
from app.modules.seguridad_accesos.cu07_clientes.services import cliente as service
from app.modules.seguridad_accesos.repositories import recuperacion_contrasena, rol
from app.modules.seguridad_accesos.services import auth

BASE = "/api/admin/clientes"
ROUTES = [("GET", BASE), ("GET", BASE + "/missing"), ("PATCH", BASE + "/missing")]


@pytest.fixture
def operator(client, registered, credentials, factory):
    with factory.begin() as db:
        db.add(Rol(nro="gestor-clientes", descripcion="Gestor CU07"))
        db.add(Funcion(id="CU07", descripcion="Gestionar Clientes"))
        db.flush()
        db.add(RolFuncion(nrorol="gestor-clientes", idfun="CU07", descripcion="Prueba"))
        user = db.get(Usuario, registered["idUsuario"])
        db.delete(db.get(Cliente, user.idusuario))
        user.tipo, user.nrorol = "A", "gestor-clientes"
        db.add(Admin(idusuario=user.idusuario, cod_adm="ADM001"))
    assert client.post("/api/auth/login", json=credentials).status_code == 200
    return registered["idUsuario"]


@pytest.fixture
def customer(client, operator, registration):
    response = client.post("/api/auth/registro", json=registration | {"correo": "cliente@example.com"})
    assert response.status_code == 201
    response = client.get(BASE + "/" + response.json()["idUsuario"])
    assert response.status_code == 200
    return response.json()


def audit(factory):
    with factory() as db:
        return [(r.accion, r.usuario_id, r.detalles) for r in db.scalars(select(Bitacora).order_by(Bitacora.id))]


def seed_credentials(factory, user_id):
    now = utcnow()
    with factory.begin() as db:
        for i in range(2):
            db.add(RecuperacionContrasena(usuario_id=user_id, token_digest=digest(f"synthetic-recovery-{i}"),
                                         creada_en=now, expira_en=now + timedelta(hours=1)))


def credential_states(factory, user_id):
    with factory() as db:
        recovery = list(db.scalars(select(RecuperacionContrasena.utilizada_en).where(
            RecuperacionContrasena.usuario_id == user_id)))
        return recovery


@pytest.mark.parametrize("method,path", ROUTES)
def test_all_routes_require_authentication(client, method, path):
    assert client.request(method, path).status_code == 401


@pytest.mark.parametrize("method,path", ROUTES)
def test_all_routes_require_cu07(client, registered, credentials, method, path):
    assert client.post("/api/auth/login", json=credentials).status_code == 200
    assert client.request(method, path).status_code == 403


@pytest.mark.parametrize("permission", [None, "CU05", "CU06"])
def test_role_name_and_other_permissions_do_not_grant_access(client, operator, factory, permission):
    with factory.begin() as db:
        db.add(Rol(nro="superadmin", descripcion="SuperAdmin"))
        db.flush()
        db.get(Usuario, operator).nrorol = "superadmin"
        if permission:
            db.add(Funcion(id=permission, descripcion="Prueba"))
            db.flush()
            db.add(RolFuncion(nrorol="superadmin", idfun=permission, descripcion="Prueba"))
    assert client.get(BASE).status_code == 403


def test_cu07_only_and_no_cu06_catalog_dependency(client, operator, customer, factory):
    with factory() as db:
        assert db.get(Funcion, "CU06") is None
    assert client.get(BASE).status_code == 200
    assert client.get("/api/admin/usuarios").status_code == 403
    assert client.get("/api/admin/roles").status_code == 403
    assert client.patch(BASE + "/" + customer["idUsuario"], json={"telefono": "123"}).status_code == 200


def test_list_pagination_search_and_literal_wildcards(client, customer, registration):
    assert client.post("/api/auth/registro", json=registration | {"correo": "otro@example.com", "nombres": "Zoe"}).status_code == 201
    result = client.get(BASE).json()
    assert result["total"] == 2 and result["offset"] == 0 and result["limit"] == 20
    assert all(row["tipo"] == "C" for row in result["items"])
    assert client.get(BASE + "?offset=1&limit=1").json()["items"] == result["items"][1:2]
    assert client.get(BASE + "?offset=999").json()["items"] == []
    for q in (" CLIENTE@EXAMPLE.COM ", "ANA", "PÉREZ", "GÓMEZ", "1234567"):
        assert client.get(BASE, params={"q": q}).json()["total"] >= 1
    for q in ("%", "_", "missing"):
        assert client.get(BASE, params={"q": q}).json()["total"] == 0
    assert all("activo" not in row for row in result["items"])


@pytest.mark.parametrize("params", [{"offset": -1}, {"limit": 0}, {"limit": 101}, {"q": "x" * 101}])
def test_query_validation(client, operator, params):
    assert client.get(BASE, params=params).status_code == 422


def test_registered_detail_and_no_admin_or_missing_target(client, customer, operator):
    assert customer["cliente"]["estado"] == "frecuente"
    assert len(customer["cliente"]["cod_cl"]) == 10
    assert "activo" not in customer and customer["rol"]["nro"] == "cliente"
    for target in (operator, "missing"):
        path = BASE + "/" + target
        assert client.get(path).status_code == 404
        assert client.patch(path, json={"nombres": "No"}).status_code == 404
        assert client.patch(path + "/estado-cuenta", json={"activo": False}).status_code == 404


def test_personal_fields_edit_and_specific_audit(client, customer, operator, factory):
    path = BASE + "/" + customer["idUsuario"]
    body = {"ci": "234", "nombres": " Nuevo ", "apellidoPat": "Otro", "apellidoMat": "Apellido",
            "sexo": "M", "correo": " NUEVO@EXAMPLE.COM ", "telefono": "555", "direccion": "Otra dirección",
            "fechaNac": "1999-02-03"}
    result = client.patch(path, json=body)
    assert result.status_code == 200
    expected = body | {"nombres": "Nuevo", "correo": "nuevo@example.com"}
    assert all(result.json()[k] == v for k, v in expected.items())
    for key in ("tipo", "nroRol", "rol", "cliente", "idUsuario"):
        assert result.json()[key] == customer[key]
    assert audit(factory)[-1] == ("cliente_actualizado", operator, {"resultado": "exito"})


@pytest.mark.parametrize("body", [
    {}, {"nombres": None}, {"nombres": " "}, {"ci": "x" * 101}, {"nombres": "x" * 101},
    {"apellidoPat": "x" * 51}, {"apellidoMat": "x" * 51}, {"telefono": "x" * 21},
    {"direccion": "x" * 151}, {"correo": "invalid"}, {"correo": "x" * 101 + "@example.com"},
    {"sexo": "X"}, {"fechaNac": "2999-01-01"}, {"fechaNac": "not-a-date"},
    {"tipo": "A"}, {"nroRol": "gestor-clientes"}, {"permisos": ["CU07"]},
    {"contrasena": "private-marker"}, {"activo": False}, {"cod_cl": "manual"},
    {"idUsuario": "manual"}, {"estado": "casual"}, {"proporciones": {"test": 1}},
])
def test_edit_allowlist_and_rollback(client, customer, factory, body):
    before = audit(factory)
    path = BASE + "/" + customer["idUsuario"]
    result = client.patch(path, json=body)
    assert result.status_code == 422 and result.json()["error"]["code"] == "datos_invalidos"
    assert "private-marker" not in result.text
    assert client.get(path).json() == customer and audit(factory) == before


@pytest.mark.parametrize("body", [{}, {"activo": None}, {"activo": "false"}, {"activo": 0},
                                   {"activo": False, "estado": "inactivo"}])
def test_state_endpoint_retired_for_all_payloads(client, customer, body):
    assert client.patch(BASE + "/" + customer["idUsuario"] + "/estado-cuenta", json=body).status_code == 404


def test_nullable_legacy_names_and_nonunique_ci_code(client, customer, factory, registration):
    with factory.begin() as db:
        db.get(Usuario, customer["idUsuario"]).nombre = None
    path = BASE + "/" + customer["idUsuario"]
    assert client.get(path).json()["nombres"] is None
    assert client.patch(path, json={"telefono": "999"}).json()["nombres"] is None
    other = client.post("/api/auth/registro", json=registration | {"correo": "otro@example.com"}).json()["idUsuario"]
    with factory.begin() as db:
        db.get(Cliente, other).cod_cl = customer["cliente"]["cod_cl"]
    assert client.get(BASE).json()["total"] == 2


@pytest.mark.parametrize("duplicate_kind", ["internal", "client"])
def test_global_normalized_email_uniqueness(client, customer, factory, operator, registration, duplicate_kind):
    target = operator
    if duplicate_kind == "client":
        target = client.post("/api/auth/registro", json=registration | {"correo": "otro@example.com"}).json()["idUsuario"]
    with factory.begin() as db:
        db.get(Usuario, target).correo = " DUPLICADO@EXAMPLE.COM "
    before = audit(factory)
    response = client.patch(BASE + "/" + customer["idUsuario"], json={"correo": " duplicado@example.com "})
    assert response.status_code == 409 and response.json()["error"]["code"] == "correo_duplicado"
    assert audit(factory) == before


def test_email_change_invalidates_recovery_not_sessions_and_new_login(client, customer, factory, registration, app):
    user_id = customer["idUsuario"]
    seed_credentials(factory, user_id)
    with closing(TestClient(app)) as other:
        other.headers.update({"Origin": "http://localhost:4200", "X-CSRF-Protection": "1"})
        login = {"correo": "cliente@example.com", "contrasena": registration["contrasena"]}
        assert other.post("/api/auth/login", json=login).status_code == 200
        assert client.patch(BASE + "/" + user_id, json={"correo": " NUEVO@EXAMPLE.COM "}).status_code == 200
        assert all(value is not None for value in credential_states(factory, user_id))
        assert other.get("/api/auth/me").status_code == 200
        assert other.post("/api/auth/login", json=login).status_code == 401
        assert other.post("/api/auth/login", json=login | {"correo": " NUEVO@EXAMPLE.COM "}).status_code == 200


def test_noop_no_dml_audit_or_credential_invalidation(client, customer, factory):
    user_id = customer["idUsuario"]
    seed_credentials(factory, user_id)
    before = audit(factory)
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            statements.append(statement)
    engine = factory.kw["bind"]
    event.listen(engine, "before_cursor_execute", capture)
    try:
        assert client.patch(BASE + "/" + user_id, json={"correo": " CLIENTE@EXAMPLE.COM "}).status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert statements == [] and audit(factory) == before
    assert credential_states(factory, user_id) == [None, None]


def test_retired_account_state_does_not_modify_data_or_recoveries(client, customer, factory, operator):
    user_id = customer["idUsuario"]
    seed_credentials(factory, user_id)
    before = audit(factory)
    path = BASE + "/" + user_id + "/estado-cuenta"
    for active in (False, True):
        assert client.patch(path, json={"activo": active}).status_code == 404
    assert client.get(BASE + "/" + user_id).json() == customer
    assert credential_states(factory, user_id) == [None, None]
    assert audit(factory) == before


def test_password_change_invalidates_customer_access(client, customer, factory, app, registration):
    with closing(TestClient(app)) as other:
        other.headers.update({"Origin": "http://localhost:4200", "X-CSRF-Protection": "1"})
        login = {"correo": "cliente@example.com", "contrasena": registration["contrasena"]}
        assert other.post("/api/auth/login", json=login).status_code == 200
        with factory.begin() as db:
            db.get(Usuario, customer["idUsuario"]).contrasena = app.state.passwords.hash("Otra contraseña de prueba")
        assert other.get("/api/auth/me").status_code == 401
        assert client.get(BASE).status_code == 200


@pytest.mark.parametrize("commercial_state", ["frecuente", "casual", "inactivo"])
def test_commercial_state_is_readonly_and_does_not_block_login(client, customer, factory, app, registration, commercial_state):
    with factory.begin() as db:
        db.get(Cliente, customer["idUsuario"]).estado = commercial_state
    path = BASE + "/" + customer["idUsuario"]
    assert client.get(path).json()["cliente"]["estado"] == commercial_state
    with closing(TestClient(app)) as other:
        other.headers.update({"Origin": "http://localhost:4200", "X-CSRF-Protection": "1"})
        assert other.post("/api/auth/login", json={"correo": "cliente@example.com", "contrasena": registration["contrasena"]}).status_code == 200


@pytest.mark.parametrize("proportions", [{"opaque": [1, 2]}, [1, "opaque"], 42, "opaque", True, None])
def test_proportions_never_exposed_or_changed(client, customer, factory, proportions):
    user_id = customer["idUsuario"]
    with factory.begin() as db:
        db.get(Cliente, user_id).proporciones = proportions
    path = BASE + "/" + user_id
    responses = [client.get(BASE), client.get(path), client.patch(path, json={"telefono": "222"})]
    assert all(r.status_code == 200 and "proporciones" not in r.text for r in responses)
    assert client.patch(path, json={"proporciones": proportions}).status_code == 422
    with factory() as db:
        assert db.get(Cliente, user_id).proporciones == proportions


@pytest.mark.parametrize("kind", ["missing_client", "non_c", "admin_cross", "employee_cross", "wrong_role",
                                  "public_functions", "internal_public_role", "missing_public_role"])
def test_incoherent_profiles_and_roles_controlled_no_repair(client, customer, factory, operator, app, kind):
    user_id = customer["idUsuario"]
    with factory.begin() as db:
        if kind == "missing_client":
            db.delete(db.get(Cliente, user_id))
        elif kind == "non_c":
            db.get(Usuario, user_id).tipo = "E"
        elif kind == "admin_cross":
            db.add(Admin(idusuario=user_id, cod_adm="ADM002"))
        elif kind == "employee_cross":
            db.add(Ciudad(id=1, nombre="Prueba"))
            db.flush()
            db.add(Sucursal(nro=1, nombre="Prueba", direccion="Prueba", idciud=1))
            db.flush()
            db.add(Empleado(idusuario=user_id, cod_emp="EMP001", cargo="Prueba", nrosuc=1))
        elif kind == "wrong_role":
            db.get(Usuario, user_id).nrorol = "gestor-clientes"
        elif kind == "public_functions":
            db.add(RolFuncion(nrorol="cliente", idfun="CU07", descripcion="Legacy"))
        elif kind == "internal_public_role":
            # A separate malformed internal user; don't remove the operator's permission.
            user = db.get(Usuario, user_id)
            values = {c.name: getattr(user, c.name) for c in Usuario.__table__.columns}
            db.add(Usuario(**(values | {"idusuario": "internal-legacy", "correo": "legacy@example.com", "tipo": "A"})))
        else:
            app.state.settings.cliente_rol_id = "missing"
    before = audit(factory)
    path = BASE + "/" + user_id
    for response in (client.get(BASE), client.get(path), client.patch(path, json={"nombres": "No persistir"})):
        assert response.status_code == 409 and response.json()["error"]["code"] == "perfil_incoherente"
    assert audit(factory) == before
    with factory() as db:
        assert db.get(Usuario, user_id).nombre == customer["nombres"]
        if kind == "missing_client":
            assert db.get(Cliente, user_id) is None


def test_inconsistent_candidates_detected_on_page_not_hidden(client, customer, factory, registration):
    other = client.post("/api/auth/registro", json=registration | {"correo": "z@example.com", "apellidoPat": "Z"}).json()["idUsuario"]
    with factory.begin() as db:
        db.delete(db.get(Cliente, other))
    first = client.get(BASE + "?limit=1")
    assert first.status_code == 200 and first.json()["total"] == 2
    assert first.json()["items"][0]["idUsuario"] == customer["idUsuario"]
    assert client.get(BASE + "?offset=1&limit=1").status_code == 409
    assert client.get(BASE, params={"q": "z@example.com"}).status_code == 409


def test_configurable_public_role_not_literal(client, customer, factory, app):
    with factory.begin() as db:
        db.add(Rol(nro="publico-custom", descripcion="Público"))
        db.flush()
        db.get(Usuario, customer["idUsuario"]).nrorol = "publico-custom"
    app.state.settings.cliente_rol_id = "publico-custom"
    path = BASE + "/" + customer["idUsuario"]
    assert client.get(path).json()["nroRol"] == "publico-custom"
    assert client.get(BASE).status_code == 200
    assert client.patch(path, json={"telefono": "333"}).status_code == 200


@pytest.mark.parametrize("operation", ["edit", "email"])
@pytest.mark.parametrize("failure", ["audit", "integrity", "recovery"])
def test_atomic_rollback_no_sensitive_errors(client, customer, factory, monkeypatch, caplog, operation, failure):
    user_id = customer["idUsuario"]
    seed_credentials(factory, user_id)
    before = audit(factory)
    original_record = service.record
    original_invalidate = recuperacion_contrasena.invalidate
    def fail_record(*args):
        original_record(*args)
        if failure == "integrity":
            raise IntegrityError("private-marker", {}, Exception("private-marker"))
        raise RuntimeError("private-marker")
    def fail_recovery(*args):
        original_invalidate(*args)
        raise RuntimeError("private-marker")
    if failure == "recovery" and operation != "edit":
        monkeypatch.setattr(recuperacion_contrasena, "invalidate", fail_recovery)
    else:
        monkeypatch.setattr(service, "record", fail_record)
    path = BASE + "/" + user_id
    body = {"nombres": "No persistir"} if operation == "edit" else {"correo": "nuevo@example.com"}
    response = client.patch(path, json=body)
    assert response.status_code == (409 if failure == "integrity" else 500)
    assert "private-marker" not in response.text + caplog.text
    assert client.get(path).json() == customer and audit(factory) == before
    assert credential_states(factory, user_id) == [None, None]


@pytest.mark.parametrize("change,status", [("role", 403), ("permission", 403)])
def test_actor_revalidated_after_locks(client, customer, factory, operator, monkeypatch, change, status):
    original = service.public_role
    def after_wait(db, settings):
        result = original(db, settings)
        if change == "role":
            db.execute(update(Usuario).where(Usuario.idusuario == operator).values(nrorol="cliente")
                       .execution_options(synchronize_session=False))
        else:
            db.delete(db.get(RolFuncion, ("gestor-clientes", "CU07")))
        db.flush()
        return result
    monkeypatch.setattr(service, "public_role", after_wait)
    before = audit(factory)
    path = BASE + "/" + customer["idUsuario"]
    assert client.patch(path, json={"nombres": "No"}).status_code == status
    assert audit(factory) == before
    with factory() as db:
        assert db.get(Usuario, customer["idUsuario"]).nombre == customer["nombres"]


def test_lock_contract_refresh_and_no_continuity_lock(factory, customer, client, monkeypatch):
    forbidden = Mock(side_effect=AssertionError("CU06 continuity not needed"))
    monkeypatch.setattr(continuidad, "lock", forbidden)
    db = Mock()
    repository.get(db, "synthetic-user", lock=True)
    query = db.scalar.call_args.args[0]
    assert "FOR UPDATE" in str(query.compile(dialect=postgresql.dialect()))
    assert query.get_execution_options()["populate_existing"] is True
    with factory() as session:
        cached = session.get(Usuario, customer["idUsuario"])
        session.execute(update(Usuario).where(Usuario.idusuario == cached.idusuario).values(telefono="Updated")
                        .execution_options(synchronize_session=False))
        assert repository.get(session, cached.idusuario, lock=True).telefono == "Updated"
        session.rollback()
    assert client.patch(BASE + "/" + customer["idUsuario"], json={"telefono": "Updated"}).status_code == 200
    forbidden.assert_not_called()


def test_csrf_cors_no_store_and_namespace(client, customer):
    path = BASE + "/" + customer["idUsuario"]
    for target, body in ((path, {"telefono": "123"}), (path + "/estado-cuenta", {"activo": False})):
        assert client.patch(target, json=body, headers={"Origin": "https://evil.example"}).status_code == 403
    client.headers.pop("X-CSRF-Protection")
    assert client.patch(path, json={"telefono": "123"}).status_code == 403
    response = client.options(path, headers={"Access-Control-Request-Method": "PATCH"})
    assert response.status_code == 204
    assert response.headers["access-control-allow-methods"] == "GET, PATCH, OPTIONS"
    assert response.headers["access-control-allow-credentials"] == "true"
    assert client.options(path, headers={"Origin": "https://evil.example"}).status_code == 403
    for target in (BASE, path, BASE + "/missing"):
        response = client.get(target)
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["pragma"] == "no-cache"
        assert response.headers["x-content-type-options"] == "nosniff"
    assert client.options("/api/admin/clientes-ajenos").status_code != 204


def test_contract_no_creation_delete_commercial_filter_or_proportions(app, client, customer, factory, caplog):
    schema = app.openapi()
    paths = schema["paths"]
    assert set(paths[BASE]) == {"get"}
    assert set(paths[BASE + "/{idUsuario}"]) == {"get", "patch"}
    assert BASE + "/{idUsuario}/estado-cuenta" not in paths
    params = {p["name"] for p in paths[BASE]["get"]["parameters"]}
    assert params == {"offset", "limit", "q"}
    for name in ("ClienteDetalle", "ClientePerfil", "ClienteEditar"):
        assert "proporciones" not in schema["components"]["schemas"][name]["properties"]
    assert client.post(BASE, json={}).status_code == 405
    assert client.delete(BASE + "/" + customer["idUsuario"]).status_code == 405
    output = client.get(BASE).text + client.get(BASE + "/" + customer["idUsuario"]).text + json.dumps(audit(factory)) + caplog.text
    assert not any(word in output for word in ("contrasena", "digest", "$argon2", "proporciones"))


def test_existing_models_match_schema_without_connection():
    ddl = str(CreateTable(Cliente.__table__).compile(dialect=postgresql.dialect()))
    assert "PRIMARY KEY (idusuario)" in ddl and "cod_cl VARCHAR(10) NOT NULL" in ddl
    assert "JSONB" in ddl and "UNIQUE" not in ddl
    assert "'frecuente', 'casual', 'inactivo'" in ddl
