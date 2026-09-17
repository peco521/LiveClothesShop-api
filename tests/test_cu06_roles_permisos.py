"""CU06 and shared continuity policy; SQLite only, not PostgreSQL concurrency proof."""
from contextlib import closing
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from app.core.errors import DomainError
from app.modules.seguridad_accesos.models import Bitacora, Funcion, Rol, RolFuncion, Usuario
from app.modules.seguridad_accesos.cu06_roles_permisos.repositories import rol as repository
from app.modules.seguridad_accesos.cu06_roles_permisos.services import rol as service
from app.modules.seguridad_accesos.shared.repositories import continuidad as locks
from app.modules.seguridad_accesos.shared.services import continuidad
from app.modules.seguridad_accesos.cu05_usuarios_empleados.services import usuario as cu05
from test_cu05_usuarios_empleados import operator, payload  # isolated fixtures, no provisioning

ROLES = "/api/admin/roles"
FUNCTIONS = "/api/admin/funciones"
ROUTES = [("GET", ROLES), ("POST", ROLES), ("GET", ROLES + "/missing"),
          ("PATCH", ROLES + "/missing"), ("GET", FUNCTIONS),
          ("GET", ROLES + "/missing/permisos"), ("PUT", ROLES + "/missing/permisos")]


@pytest.fixture
def manager(operator, factory):
    with factory.begin() as db:
        db.add(RolFuncion(nrorol="gestor", idfun="CU06", descripcion="Existing description"))
    return operator


def audit(factory):
    with factory() as db:
        return [(row.accion, row.usuario_id, row.detalles) for row in db.scalars(select(Bitacora).order_by(Bitacora.id))]


def put(client, role, ids):
    return client.put(f"{ROLES}/{role}/permisos", json={"permisos": ids})


def employee(client, payload, *, role="roles", email="backup@example.com"):
    response = client.post("/api/admin/empleados", json=payload | {"nroRol": role, "correo": email})
    assert response.status_code == 201
    return response.json()["idUsuario"]


@pytest.mark.parametrize("method,path", ROUTES)
def test_all_endpoints_require_authentication(client, method, path):
    assert client.request(method, path).status_code == 401


@pytest.mark.parametrize("method,path", ROUTES)
def test_cu05_alone_cannot_access_cu06(client, operator, method, path):
    assert client.request(method, path).status_code == 403


def test_list_detail_functions_and_metadata(client, manager, factory):
    with factory.begin() as db:
        db.add(Funcion(id="Otra", descripcion=None))
    rows = client.get(ROLES).json()
    assert rows["total"] == 5 and rows["offset"] == 0 and rows["limit"] == 20
    assert [row["nro"] for row in rows["items"]] == sorted(row["nro"] for row in rows["items"])
    assert client.get(ROLES + "?offset=1&limit=1").json()["items"] == rows["items"][1:2]
    assert client.get(ROLES + "?offset=999").json()["items"] == []
    public = client.get(ROLES + "/cliente").json()
    assert public == {"nro": "cliente", "descripcion": "Cliente", "esRolCliente": True}
    assert client.get(ROLES + "/gestor").json()["esRolCliente"] is False
    assert client.get(ROLES + "/cliente/permisos").json() == {
        "nroRol": "cliente", "esRolCliente": True, "permisos": []}
    assert {row["id"]: row["descripcion"] for row in client.get(FUNCTIONS).json()}["Otra"] is None
    for suffix in ("", "/permisos"):
        assert client.get(ROLES + "/absent" + suffix).status_code == 404


def test_metadata_uses_configuration_not_literal(client, manager, app, factory):
    with factory.begin() as db:
        db.add(Rol(nro="publico-custom", descripcion="Público"))
    app.state.settings.cliente_rol_id = "publico-custom"
    assert client.get(ROLES + "/publico-custom").json()["esRolCliente"] is True
    assert client.get(ROLES + "/cliente").json()["esRolCliente"] is False
    assert put(client, "publico-custom", ["CU05"]).status_code == 422
    assert put(client, "publico-custom", []).status_code == 200


def test_create_edit_pk_immutable_and_audit(client, manager, factory):
    response = client.post(ROLES, json={"nro": "MiRol", "descripcion": " Descripción "})
    assert response.status_code == 201
    assert response.json() == {"nro": "MiRol", "descripcion": "Descripción", "esRolCliente": False}
    assert client.get(ROLES + "/mirol").status_code == 404
    assert client.post(ROLES, json={"nro": "MiRol", "descripcion": "Otro"}).status_code == 409
    assert client.post(ROLES, json={"nro": "OtroRol", "descripcion": "Descripción"}).status_code == 201
    assert client.patch(ROLES + "/MiRol", json={"descripcion": "Actualizada"}).status_code == 200
    before = audit(factory)
    assert client.patch(ROLES + "/MiRol", json={"nro": "MiRol", "descripcion": "No"}).status_code == 422
    assert client.patch(ROLES + "/MiRol", json={"descripcion": " Actualizada "}).status_code == 200
    assert audit(factory) == before
    assert before[-1] == ("rol_actualizado", manager, {"resultado": "exito", "rol": "MiRol"})
    assert before[-3] == ("rol_creado", manager, {"resultado": "exito", "rol": "MiRol"})


@pytest.mark.parametrize("body", [{}, {"nro": "x"}, {"nro": "", "descripcion": "ok"},
    {"nro": "x" * 16, "descripcion": "ok"}, {"nro": "x", "descripcion": "x" * 51},
    {"nro": "x", "descripcion": " "}, {"nro": "x", "descripcion": None},
    {"nro": 123, "descripcion": "ok"}, {"nro": " x", "descripcion": "ok"},
    {"nro": "a/b", "descripcion": "ok"}, {"nro": "a%2Fb", "descripcion": "ok"},
    {"nro": "..", "descripcion": "ok"}, {"nro": "a?b", "descripcion": "ok"},
    {"nro": "a\\b", "descripcion": "ok"}, {"nro": "a\nb", "descripcion": "ok"},
    {"nro": "x", "descripcion": "ok", "permisos": ["CU06"]}])
def test_create_validation(client, manager, body, factory):
    before = audit(factory)
    assert client.post(ROLES, json=body).status_code == 422
    assert audit(factory) == before


def test_exact_limits_and_unicode_identity(client, manager):
    role_id = "Á" * 15
    assert client.post(ROLES, json={"nro": role_id, "descripcion": "x" * 50}).status_code == 201
    assert client.get(ROLES + "/" + role_id).json()["nro"] == role_id
    assert client.patch(ROLES + "/" + role_id, json={"descripcion": "x" * 51}).status_code == 422


@pytest.mark.parametrize("query", ["offset=-1", "limit=0", "limit=101"])
def test_pagination_validation(client, manager, query):
    assert client.get(ROLES + "?" + query).status_code == 422


@pytest.mark.parametrize("body", [{}, {"permisos": None}, {"permisos": "CU05"},
    {"permisos": ["CU05", "CU05"]}, {"permisos": ["CU99"]}, {"permisos": ["CU05", "missing"]},
    {"permisos": ["x" * 16]}, {"permisos": [1]}, {"permisos": [""]},
    {"permisos": [" CU05"]}, {"permisos": [], "descripcion": "No"}])
def test_invalid_permissions_atomic(client, manager, factory, body):
    before = audit(factory)
    assert client.put(ROLES + "/laboral/permisos", json=body).status_code == 422
    assert client.get(ROLES + "/laboral/permisos").json()["permisos"] == []
    assert audit(factory) == before


def test_replacement_preserves_existing_descriptions_and_catalog(client, manager, factory):
    before_catalog = client.get(FUNCTIONS).json()
    assert put(client, "laboral", ["CU06", "CU05"]).json()["permisos"] == ["CU05", "CU06"]
    with factory.begin() as db:
        rows = list(db.scalars(select(RolFuncion).where(RolFuncion.nrorol == "laboral")))
        assert all(0 < len(row.descripcion) <= 100 for row in rows)
        db.get(RolFuncion, ("laboral", "CU05")).descripcion = "Conservar"
    assert put(client, "laboral", ["CU05"]).status_code == 200
    with factory() as db:
        assert db.get(RolFuncion, ("laboral", "CU05")).descripcion == "Conservar"
    assert audit(factory)[-1] == ("permisos_rol_actualizados", manager,
        {"resultado": "exito", "rol": "laboral", "agregadas": [], "retiradas": ["CU06"]})
    assert put(client, "laboral", []).json()["permisos"] == []
    assert client.get(FUNCTIONS).json() == before_catalog
    assert put(client, "missing", []).status_code == 404


def test_noop_has_no_dml_or_audit(client, manager, factory):
    statements = []
    engine = factory.kw["bind"]
    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            statements.append(statement)
    event.listen(engine, "before_cursor_execute", capture)
    try:
        before = audit(factory)
        assert put(client, "gestor", ["CU06", "CU05"]).status_code == 200
        assert client.patch(ROLES + "/gestor", json={"descripcion": "Gestor CU05"}).status_code == 200
        assert put(client, "cliente", []).status_code == 200
        assert audit(factory) == before and statements == []
    finally:
        event.remove(engine, "before_cursor_execute", capture)


def test_public_role_never_receives_functions_and_can_be_cleaned(client, manager, factory):
    before = audit(factory)
    assert put(client, "cliente", ["CU05"]).status_code == 422
    assert audit(factory) == before
    with factory.begin() as db:
        db.add(RolFuncion(nrorol="cliente", idfun="CU05", descripcion="Legacy"))
    assert put(client, "cliente", []).status_code == 200
    assert client.get(ROLES + "/cliente/permisos").json()["permisos"] == []


@pytest.mark.parametrize("backup", ["none", "empty_role"])
def test_last_effective_user_not_last_role(client, manager, payload, factory, backup):
    before = audit(factory)
    response = put(client, "gestor", ["CU05"])
    assert response.status_code == 409 and response.json()["error"]["code"] == "ultimo_usuario_cu06"
    assert client.get(ROLES + "/gestor/permisos").json()["permisos"] == ["CU05", "CU06"]
    assert audit(factory) == before


def test_self_revocation_with_backup_and_same_session(client, manager, payload, factory):
    employee(client, payload)
    cookie_before = dict(client.cookies)
    sessions_before = dict(client.app.state.access_tokens._entries)
    assert put(client, "gestor", ["CU05"]).status_code == 200
    assert client.get(ROLES).status_code == 403
    assert client.get("/api/auth/me").json()["permisos"] == ["CU05"]
    assert dict(client.cookies) == cookie_before
    assert client.app.state.access_tokens._entries == sessions_before


def test_permission_grant_next_request_and_no_name_bypass(client, operator, factory):
    assert client.get(ROLES).status_code == 403
    with factory.begin() as db:
        db.get(Usuario, operator).nrorol = "superadmin"
    assert client.get(ROLES).status_code == 403
    with factory.begin() as db:
        db.add(RolFuncion(nrorol="superadmin", idfun="CU06", descripcion="Test"))
    assert client.get(ROLES).status_code == 200
    assert client.get("/api/admin/usuarios").status_code == 403


def test_cu05_cannot_remove_last_eligible_user(client, operator, payload, factory):
    user_id = employee(client, payload)
    before = audit(factory)
    response = client.patch(f"/api/admin/empleados/{user_id}", json={"nroRol": "laboral"})
    assert response.status_code == 409 and response.json()["error"]["code"] == "ultimo_usuario_cu06"
    assert audit(factory) == before
    with factory() as db:
        user = db.get(Usuario, user_id)
        assert user.nrorol == "roles"


def test_cu05_zero_state_recovery_noop_and_role_changes(client, operator, payload, factory):
    user_id = employee(client, payload, role="laboral")
    before = audit(factory)
    assert client.patch(f"/api/admin/empleados/{user_id}", json={"nroRol": "laboral"}).status_code == 200
    assert audit(factory) == before
    assert client.patch(f"/api/admin/empleados/{user_id}", json={"nroRol": "roles"}).status_code == 200
    employee(client, payload, email="second@example.com")
    assert client.patch(f"/api/admin/empleados/{user_id}", json={"nroRol": "laboral"}).status_code == 200


def test_admin_self_deactivation_not_supported(client, manager):
    assert client.patch(f"/api/admin/usuarios/{manager}/estado", json={"activo": False}).status_code == 404
    assert client.get(ROLES).status_code == 200


@pytest.mark.parametrize("operation", ["create", "edit", "permissions"])
def test_audit_failure_rolls_back_all_writes(client, manager, factory, monkeypatch, operation):
    before = audit(factory)
    def fail(*args, **kwargs):
        raise RuntimeError("private-error-marker")
    monkeypatch.setattr(service, "record_role", fail)
    if operation == "create":
        response = client.post(ROLES, json={"nro": "new", "descripcion": "New"})
        assert client.get(ROLES + "/new").status_code == 404
    elif operation == "edit":
        response = client.patch(ROLES + "/laboral", json={"descripcion": "Changed"})
        assert client.get(ROLES + "/laboral").json()["descripcion"] == "Empleado"
    else:
        response = put(client, "laboral", ["CU05", "CU06"])
        assert client.get(ROLES + "/laboral/permisos").json()["permisos"] == []
    assert response.status_code == 500 and "private-error-marker" not in response.text
    assert audit(factory) == before


def test_replacement_integrity_failure_rolls_back(client, manager, factory, monkeypatch):
    original = repository.replace
    def fail(db, role_id, current, desired):
        original(db, role_id, current, desired)
        db.add(RolFuncion(nrorol=role_id, idfun="not-existing", descripcion="Test"))
        db.flush()
    monkeypatch.setattr(repository, "replace", fail)
    before = audit(factory)
    assert put(client, "laboral", ["CU05"]).status_code == 409
    assert client.get(ROLES + "/laboral/permisos").json()["permisos"] == []
    assert audit(factory) == before


@pytest.mark.parametrize("method,path,body", [("POST", ROLES, {"nro": "new", "descripcion": "New"}),
    ("PATCH", ROLES + "/laboral", {"descripcion": "New"}),
    ("PUT", ROLES + "/laboral/permisos", {"permisos": []})])
def test_csrf_cors_and_no_store(client, manager, method, path, body):
    assert client.request(method, path, json=body, headers={"Origin": "https://evil.example"}).status_code == 403
    client.headers.pop("X-CSRF-Protection")
    assert client.request(method, path, json=body).status_code == 403
    response = client.options(path, headers={"Access-Control-Request-Method": method})
    assert response.status_code == 204
    assert "PUT" in response.headers["access-control-allow-methods"]
    assert response.headers["access-control-allow-credentials"] == "true"
    assert response.headers["cache-control"] == "no-store"
    assert client.options(path, headers={"Origin": "https://evil.example"}).status_code == 403
    for route in (ROLES, FUNCTIONS, ROLES + "/gestor", ROLES + "/gestor/permisos"):
        assert client.get(route).headers["cache-control"] == "no-store"


def test_no_delete_or_function_crud(app):
    paths = app.openapi()["paths"]
    assert set(paths[FUNCTIONS]) == {"get"}
    assert set(paths[ROLES]) == {"get", "post"}
    assert set(paths[ROLES + "/{nro}"]) == {"get", "patch"}
    assert set(paths[ROLES + "/{nro}/permisos"]) == {"get", "put"}


def test_orm_constraints_without_postgresql_connection():
    assert Rol.__table__.c.nro.type.length == Funcion.__table__.c.id.type.length == 15
    assert Rol.__table__.c.descripcion.type.length == 50
    assert Funcion.__table__.c.descripcion.nullable is True
    ddl = str(CreateTable(RolFuncion.__table__).compile(dialect=postgresql.dialect()))
    assert "descripcion VARCHAR(100) NOT NULL" in ddl
    assert "PRIMARY KEY (nrorol, idfun)" in ddl
    assert ddl.count("ON DELETE CASCADE ON UPDATE CASCADE") == 2


def test_pg_lock_contract_and_isolation_fail_closed():
    db = Mock()
    db.get_bind.return_value.dialect.name = "postgresql"
    db.connection.return_value.get_isolation_level.return_value = "READ COMMITTED"
    locks.lock(db)
    query = db.scalar.call_args.args[0]
    assert "FOR UPDATE" in str(query.compile(dialect=postgresql.dialect()))
    assert query.compile().params == {"id_1": "CU06"}
    assert query.get_execution_options()["populate_existing"] is True
    for level in ("REPEATABLE READ", "SERIALIZABLE"):
        db.connection.return_value.get_isolation_level.return_value = level
        with pytest.raises(DomainError) as error:
            locks.lock(db)
        assert error.value.code == "aislamiento_no_compatible"


@pytest.mark.parametrize("change,status", [("permission", 403), ("role", 403)])
def test_actor_revalidated_after_lock_before_target(client, manager, factory, monkeypatch, change, status):
    original = locks.lock
    def changed_during_wait(db):
        result = original(db)
        # Simulates the new visible state after a wait, not concurrent SQLite.
        if change == "permission":
            db.delete(db.get(RolFuncion, ("gestor", "CU06")))
        else:
            db.get(Usuario, manager).nrorol = "laboral"
        db.flush()
        return result
    monkeypatch.setattr(locks, "lock", changed_during_wait)
    before = audit(factory)
    assert put(client, "laboral", ["CU05"]).status_code == status
    assert audit(factory) == before
    with factory() as db:
        assert db.get(RolFuncion, ("laboral", "CU05")) is None


@pytest.mark.parametrize("operation", ["put", "edit_employee"])
def test_shared_lock_precedes_target_locks(client, manager, payload, monkeypatch, operation):
    user_id = employee(client, payload, role="laboral")
    calls = []
    original_lock = locks.lock
    def lock(db):
        calls.append("common")
        return original_lock(db)
    monkeypatch.setattr(locks, "lock", lock)
    if operation == "put":
        original = repository.get
        def get(db, role_id, *, lock=False):
            if lock:
                calls.append("target")
            return original(db, role_id, lock=lock)
        monkeypatch.setattr(repository, "get", get)
        response = put(client, "laboral", ["CU05"])
    else:
        original = cu05.internal
        def internal(db, user_id, *, lock=False):
            if lock:
                calls.append("target")
            return original(db, user_id, lock=lock)
        monkeypatch.setattr(cu05, "internal", internal)
        response = client.patch(f"/api/admin/empleados/{user_id}", json={"nroRol": "gestor"})
    assert response.status_code == 200
    assert calls[:2] == ["common", "target"]


def test_initial_zero_and_missing_function_do_not_block_cu05(client, operator, payload, factory):
    with factory.begin() as db:
        db.delete(db.get(RolFuncion, ("roles", "CU06")))
        db.flush()
        db.delete(db.get(Funcion, "CU06"))
    user_id = employee(client, payload, role="laboral")
    assert client.patch(f"/api/admin/empleados/{user_id}", json={"cargo": "Nuevo"}).status_code == 200


def test_two_roles_serialized_revocations_keep_last_user(client, manager, payload, app, factory):
    # Sequential outcomes required after the common lock. NOT a concurrency test.
    employee(client, payload)
    with closing(TestClient(app)) as other:
        other.headers.update({"Origin": "http://localhost:4200", "X-CSRF-Protection": "1"})
        assert other.post("/api/auth/login", json={"correo": "backup@example.com", "contrasena": payload["contrasena"]}).status_code == 200
        assert put(client, "gestor", ["CU05"]).status_code == 200
        before = audit(factory)
        assert put(other, "roles", []).status_code == 409
        assert other.get(ROLES).status_code == 200
        assert audit(factory) == before


def test_revocation_then_cu05_role_change_keeps_last_user(client, manager, payload, factory):
    user_id = employee(client, payload)
    assert put(client, "gestor", ["CU05"]).status_code == 200
    before = audit(factory)
    response = client.patch(f"/api/admin/empleados/{user_id}", json={"nroRol": "laboral"})
    assert response.status_code == 409
    assert audit(factory) == before


def test_two_users_in_same_role_are_not_backup_for_role_revocation(client, manager, payload):
    employee(client, payload, role="gestor")
    assert put(client, "gestor", ["CU05"]).status_code == 409


def test_public_role_user_never_counts_as_backup(client, manager, factory, registration):
    # Legacy contaminated public role, deliberately injected only into SQLite.
    result = client.post("/api/auth/registro", json=registration | {"correo": "public@example.com"})
    assert result.status_code == 201
    with factory.begin() as db:
        db.add(RolFuncion(nrorol="cliente", idfun="CU06", descripcion="Legacy"))
    assert put(client, "gestor", ["CU05"]).status_code == 409


def test_cu06_only_actor_can_assign_other_permissions_no_subset(client, manager, factory):
    with factory.begin() as db:
        db.delete(db.get(RolFuncion, ("gestor", "CU05")))
    assert client.get("/api/admin/usuarios").status_code == 403
    assert put(client, "laboral", ["CU05", "CU06"]).status_code == 200


@pytest.mark.parametrize("permission", ["CU05", "CU06"])
def test_revalidation_refreshes_cached_actor(factory, manager, settings, permission):
    with factory() as db:
        cached = db.get(Usuario, manager)
        assert cached.nrorol == "gestor"
        db.execute(update(Usuario).where(Usuario.idusuario == manager).values(nrorol="laboral")
                   .execution_options(synchronize_session=False))
        assert cached.nrorol == "gestor"  # ORM identity map deliberately stale.
        with pytest.raises(DomainError) as error:
            continuidad.begin_change(db, manager, permission, settings.cliente_rol_id)
        assert error.value.status == 403
        assert cached.nrorol == "laboral"
        db.rollback()


def test_role_read_after_lock_refreshes_cached_description(factory, manager):
    with factory() as db:
        cached = db.get(Rol, "laboral")
        db.execute(update(Rol).where(Rol.nro == "laboral").values(descripcion="Updated elsewhere")
                   .execution_options(synchronize_session=False))
        assert cached.descripcion == "Empleado"
        assert repository.get(db, "laboral", lock=True).descripcion == "Updated elsewhere"
        db.rollback()


def test_cu05_actor_permission_rechecked_after_lock(client, manager, payload, monkeypatch, factory):
    user_id = employee(client, payload, role="laboral")
    original = locks.lock
    def lost_permission(db):
        result = original(db)
        db.delete(db.get(RolFuncion, ("gestor", "CU05")))
        db.flush()
        return result
    monkeypatch.setattr(locks, "lock", lost_permission)
    before = audit(factory)
    response = client.patch(f"/api/admin/empleados/{user_id}", json={"nroRol": "roles"})
    assert response.status_code == 403 and audit(factory) == before
    with factory() as db:
        user = db.get(Usuario, user_id)
        assert user.nrorol == "laboral"


def test_audit_insert_then_failure_rolls_back_audit_and_permission(client, manager, factory, monkeypatch):
    original = service.record_role
    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("synthetic failure after audit flush")
    monkeypatch.setattr(service, "record_role", fail)
    before = audit(factory)
    assert put(client, "laboral", ["CU05"]).status_code == 500
    assert audit(factory) == before
    assert client.get(ROLES + "/laboral/permisos").json()["permisos"] == []


def test_long_function_description_not_copied_to_audit_or_assignment(client, manager, factory):
    code = "f" * 15
    role_id = "r" * 15
    with factory.begin() as db:
        db.add(Funcion(id=code, descripcion="free-form-marker"))
    assert client.post(ROLES, json={"nro": role_id, "descripcion": "free-form-marker"}).status_code == 201
    assert put(client, role_id, [code]).status_code == 200
    with factory() as db:
        description = db.get(RolFuncion, (role_id, code)).descripcion
        assert 0 < len(description) <= 100 and "free-form-marker" not in description
    assert "free-form-marker" not in str(audit(factory))


def test_errors_and_audit_do_not_expose_sensitive_fields(client, manager, factory, caplog):
    response = client.post(ROLES, json={"nro": "x", "descripcion": "ok", "token": "private-marker"})
    assert response.status_code == 422
    output = response.text + str(audit(factory)) + caplog.text
    assert "private-marker" not in output
    for path in (ROLES, FUNCTIONS, ROLES + "/gestor", ROLES + "/gestor/permisos"):
        output = client.get(path).text
        assert not any(key in output for key in ("contrasena", "digest", "token", "$argon2"))


def test_permissions_delete_failure_restores_removed_assignment(client, manager, factory, monkeypatch):
    assert put(client, "laboral", ["CU05", "CU06"]).status_code == 200
    before = audit(factory)
    monkeypatch.setattr(service, "record_role", Mock(side_effect=RuntimeError("synthetic failure")))
    assert put(client, "laboral", []).status_code == 500
    assert client.get(ROLES + "/laboral/permisos").json()["permisos"] == ["CU05", "CU06"]
    assert audit(factory) == before
