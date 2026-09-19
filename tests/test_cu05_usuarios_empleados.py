"""CU05 HTTP/domain regression tests. Only the injected SQLite test database."""
import json
from contextlib import closing
from datetime import timedelta

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from app.core.dependencies import require_permission
from app.core.security import digest, utcnow
from app.modules.seguridad_accesos.models import (
    Admin, Bitacora, Cliente, Funcion, RecuperacionContrasena, Rol, RolFuncion, Usuario,
)
from app.modules.seguridad_accesos.cu05_usuarios_empleados.models import Empleado
from app.modules.seguridad_accesos.cu05_usuarios_empleados.repositories import usuario as repository
from app.modules.seguridad_accesos.cu05_usuarios_empleados.services import usuario as service
from app.modules.seguridad_accesos.shared.models import Ciudad, Sucursal

USERS = "/api/admin/usuarios"
EMPLOYEES = "/api/admin/empleados"


@pytest.fixture
def operator(client, factory, registered, credentials):
    with factory.begin() as db:
        db.add_all([Rol(nro="gestor", descripcion="Gestor CU05"), Rol(nro="laboral", descripcion="Empleado"),
                    Rol(nro="superadmin", descripcion="SuperAdmin"), Rol(nro="roles", descripcion="Gestor CU06")])
        db.add_all([Funcion(id="CU05", descripcion="Usuarios"), Funcion(id="CU06", descripcion="Roles")])
        db.add(Ciudad(id=1, nombre="Ciudad de prueba"))
        db.flush()
        db.add_all([RolFuncion(nrorol="gestor", idfun="CU05", descripcion="Prueba"),
                    RolFuncion(nrorol="roles", idfun="CU06", descripcion="Prueba")])
        db.add(Sucursal(nro=1, nombre="Sucursal de prueba", direccion="Prueba", idciud=1))
        user = db.get(Usuario, registered["idUsuario"])
        db.delete(db.get(Cliente, user.idusuario))
        user.tipo, user.nrorol = "A", "gestor"
        db.add(Admin(idusuario=user.idusuario, cod_adm="ADM001"))
    assert client.post("/api/auth/login", json=credentials).status_code == 200
    return registered["idUsuario"]


@pytest.fixture
def payload(registration):
    return registration | {"correo": "empleado@example.com",
                           "cargo": "Cajero", "nroRol": "laboral", "nroSuc": 1}


@pytest.fixture
def employee(client, operator, payload):
    response = client.post(EMPLOYEES, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def count(db, model):
    return db.scalar(select(func.count()).select_from(model))


def audit(factory):
    with factory() as db:
        return [(row.accion, row.usuario_id, row.detalles) for row in db.scalars(select(Bitacora).order_by(Bitacora.id))]


ROUTES = [("GET", USERS), ("GET", USERS + "/roles"), ("GET", EMPLOYEES + "/ciudades"),
          ("GET", EMPLOYEES + "/sucursales"), ("GET", USERS + "/missing"),
          ("POST", EMPLOYEES), ("PATCH", EMPLOYEES + "/missing")]


@pytest.mark.parametrize("method,path", ROUTES)
def test_401_all_routes(client, method, path):
    assert client.request(method, path).status_code == 401


@pytest.mark.parametrize("method,path", ROUTES)
def test_403_all_routes_without_permission(client, registered, credentials, method, path):
    assert client.post("/api/auth/login", json=credentials).status_code == 200
    assert client.request(method, path).status_code == 403


def test_cu05_access_and_options(client, operator):
    assert client.get(USERS).status_code == 200
    assert client.get(USERS + "/" + operator).json()["tipo"] == "A"
    roles = client.get(USERS + "/roles").json()
    assert {role["nro"] for role in roles} == {"gestor", "laboral", "superadmin", "roles"}
    assert client.get(EMPLOYEES + "/ciudades").json() == [{"id": 1, "nombre": "Ciudad de prueba"}]
    branches = client.get(EMPLOYEES + "/sucursales", params={"idCiud": 1}).json()
    assert branches[0]["ciudad"] == {"id": 1, "nombre": "Ciudad de prueba"}
    assert branches[0]["nro"] == 1
    assert client.get(EMPLOYEES + "/sucursales?idCiud=2").json() == []


def test_list_internal_and_pagination(client, operator, employee):
    result = client.get(USERS).json()
    assert result["total"] == 2 and {row["tipo"] for row in result["items"]} == {"A", "E"}
    page = client.get(USERS + "?offset=1&limit=1").json()
    assert page["total"] == 2 and len(page["items"]) == 1 and page["offset"] == 1
    assert client.get(USERS + "?tipo=E").json()["items"][0]["idUsuario"] == employee["idUsuario"]
    assert client.get(USERS + "?q=EMPLEADO@EXAMPLE.COM").json()["total"] == 1
    assert all("activo" not in row for row in result["items"])
    assert client.get(USERS + "?q=%").json()["total"] == 0


def test_excludes_clients_everywhere(client, operator, employee, registration, credentials):
    response = client.post("/api/auth/registro", json=registration | {"correo": "cliente@example.com"})
    assert response.status_code == 201
    client_id = response.json()["idUsuario"]
    # CU01: el registro público deja iniciada la sesión del cliente nuevo; esta
    # prueba necesita la sesión administrativa del operador, así que se rehace.
    assert client.post("/api/auth/login", json=credentials).status_code == 200
    assert client.get(USERS).json()["total"] == 2
    assert client.get(USERS + "/" + client_id).status_code == 404
    assert client.patch(USERS + "/" + client_id + "/estado", json={"activo": False}).status_code == 404
    assert client.patch(EMPLOYEES + "/" + client_id, json={"cargo": "Otro"}).status_code == 404
    assert client.get(USERS + "?tipo=C").status_code == 422


def test_create_employee_and_atomic_audit(client, operator, payload, factory):
    response = client.post(EMPLOYEES, json=payload | {"correo": " EMPLEADO@EXAMPLE.COM "})
    assert response.status_code == 201
    result = response.json()
    assert result["tipo"] == "E" and "activo" not in result
    assert result["correo"] == "empleado@example.com" and result["admin"] is None
    assert result["empleado"] == {"cod_emp": "Emp-000001", "cargo": "Cajero", "nroSuc": 1}
    with factory() as db:
        assert count(db, Usuario) == 2 and count(db, Empleado) == 1 and count(db, Admin) == 1
        assert count(db, Cliente) == 0
    assert audit(factory)[-2:] == [
        ("usuario_creado", operator, {"resultado": "exito"}),
        ("empleado_creado", operator, {"resultado": "exito"}),
    ]


def test_argon2id_login(client, operator, employee, payload, factory, app):
    with factory() as db:
        encoded = db.get(Usuario, employee["idUsuario"]).contrasena
        assert encoded.startswith("$argon2id$") and encoded != payload["contrasena"]
        assert app.state.passwords.verify(encoded, payload["contrasena"])
    assert client.post("/api/auth/login", json={"correo": payload["correo"], "contrasena": payload["contrasena"]}).status_code == 200
    assert client.get(USERS).status_code == 403


def test_normalized_duplicate_email(client, operator, employee, payload, factory):
    with factory.begin() as db:
        db.get(Usuario, employee["idUsuario"]).correo = "  EMPLEADO@EXAMPLE.COM  "
    response = client.post(EMPLOYEES, json=payload)
    assert response.status_code == 409 and response.json()["error"]["code"] == "correo_duplicado"
    with factory() as db:
        assert count(db, Empleado) == 1


@pytest.mark.parametrize("changes,code", [({"nroRol": "missing"}, "rol_no_encontrado"),
                                        ({"nroSuc": 999}, "sucursal_no_encontrada"),
                                        ({"nroRol": "cliente"}, "rol_publico_no_asignable")])
def test_reference_validation(client, operator, payload, factory, changes, code):
    before = audit(factory)
    response = client.post(EMPLOYEES, json=payload | changes)
    assert response.status_code == 422 and response.json()["error"]["code"] == code
    with factory() as db:
        assert count(db, Usuario) == 1 and count(db, Empleado) == 0
    assert audit(factory) == before


def test_edit_employee(client, employee, factory, operator):
    response = client.patch(EMPLOYEES + "/" + employee["idUsuario"], json={
        "nombres": "Nuevo Nombre", "correo": " NUEVO@EXAMPLE.COM ", "cargo": "Encargado"})
    assert response.status_code == 200
    result = response.json()
    assert result["correo"] == "nuevo@example.com" and result["nombres"] == "Nuevo Nombre"
    assert result["empleado"]["cargo"] == "Encargado" and result["tipo"] == "E"
    assert result["empleado"]["cod_emp"] == employee["empleado"]["cod_emp"]
    assert [event[0] for event in audit(factory)[-2:]] == ["usuario_actualizado", "empleado_actualizado"]
    assert client.patch(EMPLOYEES + "/" + operator, json={"nombres": "No"}).status_code == 404


@pytest.mark.parametrize("changes,status", [({"correo": "ANA@EXAMPLE.COM"}, 409),
                                         ({"nroRol": "missing"}, 422), ({"nroSuc": 999}, 422),
                                         ({"nroRol": "cliente"}, 422), ({"tipo": "A"}, 422),
                                         ({"contrasena": "No cambiar password"}, 422),
                                         ({"activo": False}, 422), ({"cargo": None}, 422), ({}, 422)])
def test_invalid_edit_does_not_change_data(client, employee, factory, changes, status):
    before = audit(factory)
    url = USERS + "/" + employee["idUsuario"]
    assert client.patch(EMPLOYEES + "/" + employee["idUsuario"], json=changes).status_code == status
    assert client.get(url).json() == employee
    assert audit(factory) == before


def test_state_endpoint_retired_without_changing_employee(client, employee, factory, operator):
    before = audit(factory)
    path = USERS + "/" + employee["idUsuario"] + "/estado"
    for active in (False, True):
        assert client.patch(path, json={"activo": active}).status_code == 404
    assert client.get(USERS + "/" + employee["idUsuario"]).json() == employee
    assert audit(factory) == before


def test_admin_state_endpoint_retired(client, operator, factory):
    assert client.patch(USERS + "/" + operator + "/estado", json={"activo": False}).status_code == 404
    assert client.get(USERS).status_code == 200
    with factory() as db:
        assert db.get(Admin, operator) is not None


def test_no_sensitive_output_or_logs(client, operator, employee, factory, payload, caplog):
    with factory() as db:
        encoded = db.get(Usuario, employee["idUsuario"]).contrasena
    output = json.dumps(employee) + client.get(USERS).text + client.get(USERS + "/" + employee["idUsuario"]).text
    output += json.dumps(audit(factory)) + caplog.text
    assert payload["contrasena"] not in output and encoded not in output
    assert not any(word in output.lower() for word in ("contrasena", "digest", "$argon2", '"hash"'))
    for path in (USERS, USERS + "/roles", EMPLOYEES + "/ciudades", EMPLOYEES + "/sucursales"):
        assert client.get(path).headers["cache-control"] == "no-store"


@pytest.mark.parametrize("failure", ["profile", "audit"])
def test_create_rollback_including_first_audit(client, operator, payload, factory, monkeypatch, caplog, failure):
    before = audit(factory)
    def fail(*args, **kwargs):
        raise RuntimeError(payload["contrasena"])
    if failure == "profile":
        monkeypatch.setattr(repository, "add_employee", fail)
    else:
        original = service.record
        def record(db, action, *args):
            if action == "empleado_creado":
                fail()
            original(db, action, *args)
        monkeypatch.setattr(service, "record", record)
    response = client.post(EMPLOYEES, json=payload)
    assert response.status_code == 500
    assert payload["contrasena"] not in response.text + caplog.text
    with factory() as db:
        assert count(db, Usuario) == 1 and count(db, Empleado) == 0
    assert audit(factory) == before


def test_real_profile_constraint_failure_rolls_back(client, operator, payload, factory, monkeypatch):
    original = repository.add_employee
    def broken(db, employee):
        employee.nrosuc = 999
        original(db, employee)
    monkeypatch.setattr(repository, "add_employee", broken)
    before = audit(factory)
    assert client.post(EMPLOYEES, json=payload).status_code == 409
    with factory() as db:
        assert count(db, Usuario) == 1 and count(db, Empleado) == 0
    assert audit(factory) == before


def test_update_rollback(client, employee, factory, monkeypatch):
    before = audit(factory)
    def fail(*args):
        raise RuntimeError("synthetic audit failure")
    monkeypatch.setattr(service, "record", fail)
    user_id = employee["idUsuario"]
    now = utcnow()
    with factory.begin() as db:
        db.add(RecuperacionContrasena(usuario_id=user_id, token_digest=digest("rollback-recovery"),
                                     creada_en=now, expira_en=now + timedelta(hours=1)))
    response = client.patch(EMPLOYEES + "/" + user_id, json={"nombre": "No persistir", "cargo": "No persistir"})
    assert response.status_code == 500
    assert client.get(USERS + "/" + user_id).json() == employee
    assert audit(factory) == before
    with factory() as db:
        assert db.scalar(select(RecuperacionContrasena).where(RecuperacionContrasena.usuario_id == user_id)).utilizada_en is None


@pytest.mark.parametrize("kind", ["missing_employee", "employee_admin", "employee_client", "admin_employee", "missing_admin"])
def test_incoherent_profiles_rejected_without_repair(client, employee, operator, factory, kind):
    target = employee["idUsuario"]
    with factory.begin() as db:
        if kind == "missing_employee":
            db.delete(db.get(Empleado, target))
        elif kind == "employee_admin":
            db.add(Admin(idusuario=target, cod_adm="ADM002"))
        elif kind == "employee_client":
            db.add(Cliente(idusuario=target, cod_cl="CL002"))
        elif kind == "admin_employee":
            target = operator
            db.add(Empleado(idusuario=target, cod_emp="EMP003", cargo="Prueba", nrosuc=1))
        else:
            target = operator
            db.delete(db.get(Admin, target))
    before = audit(factory)
    assert client.get(USERS + "/" + target).status_code == 409
    assert client.get(USERS).status_code == 409
    assert client.patch(EMPLOYEES + "/" + target, json={"cargo": "No"}).status_code == 409
    assert audit(factory) == before


def test_superadmin_name_has_no_bypass(client, operator, factory):
    with factory.begin() as db:
        db.get(Usuario, operator).nrorol = "superadmin"
    assert client.get(USERS).status_code == 403
    with factory.begin() as db:
        db.add(RolFuncion(nrorol="superadmin", idfun="CU05", descripcion="Prueba"))
    assert client.get(USERS).status_code == 200


def test_cu05_does_not_grant_cu06_or_modify_catalog(client, operator, factory, app, payload):
    @app.get("/api/test-only-cu06")
    def cu06(identity=Depends(require_permission("CU06"))):
        return {"ok": True}
    with factory() as db:
        before = tuple(count(db, model) for model in (Rol, Funcion, RolFuncion))
    assert client.get("/api/test-only-cu06").status_code == 403
    response = client.post(EMPLOYEES, json=payload)
    assert response.status_code == 201
    assert client.get("/api/test-only-cu06").status_code == 403
    assert client.post(USERS + "/roles", json={"nro": "new", "descripcion": "No"}).status_code == 405
    with factory() as db:
        assert tuple(count(db, model) for model in (Rol, Funcion, RolFuncion)) == before


def test_assign_existing_role_without_invented_subset_policy(client, operator, payload):
    response = client.post(EMPLOYEES, json=payload | {"nroRol": "roles"})
    assert response.status_code == 201 and response.json()["nroRol"] == "roles"
    # C continuity is not a subset policy: keep another eligible CU06 user.
    assert client.post(EMPLOYEES, json=payload | {"nroRol": "roles", "correo": "backup@example.com"}).status_code == 201
    response = client.patch(EMPLOYEES + "/" + response.json()["idUsuario"], json={"nroRol": "superadmin"})
    assert response.status_code == 200 and response.json()["nroRol"] == "superadmin"


@pytest.mark.parametrize("field,value", [("tipo", "A"), ("activo", False), ("permisos", ["CU06"]),
                                        ("idUsuario", "manual"), ("cod_emp", "X" * 11),
                                        ("cargo", "X" * 51), ("nroSuc", True), ("contrasena", "short")])
def test_create_validation_allowlist(client, operator, payload, field, value):
    assert client.post(EMPLOYEES, json=payload | {field: value}).status_code == 422


def test_no_unique_ci_or_employee_code_invented(client, operator, employee, payload):
    assert client.post(EMPLOYEES, json=payload | {"correo": "second@example.com"}).status_code == 201


def test_csrf_and_cors(client, operator, payload, employee):
    assert client.post(EMPLOYEES, json=payload, headers={"Origin": "https://evil.example"}).status_code == 403
    path = EMPLOYEES + "/" + employee["idUsuario"]
    client.headers.pop("X-CSRF-Protection")
    assert client.patch(path, json={"cargo": "Otro"}).status_code == 403
    response = client.options(path, headers={"Access-Control-Request-Method": "PATCH"})
    assert response.status_code == 204
    assert "PATCH" in response.headers["access-control-allow-methods"]
    assert response.headers["access-control-allow-credentials"] == "true"
    assert response.headers["cache-control"] == "no-store"
    assert client.options(path, headers={"Origin": "https://evil.example"}).status_code == 403


def test_models_match_official_schema_without_postgresql_connection():
    ddl = str(CreateTable(Empleado.__table__).compile(dialect=postgresql.dialect()))
    assert "cod_emp VARCHAR(10) NOT NULL" in ddl and "cargo VARCHAR(50) NOT NULL" in ddl
    assert "UNIQUE" not in ddl and ddl.count("ON DELETE CASCADE ON UPDATE CASCADE") == 2
    assert Ciudad.__table__.c.id.autoincrement is False
    assert Sucursal.__table__.c.idciud.type.python_type is int


def test_no_admin_creation_or_physical_delete_routes(app):
    paths = app.openapi()["paths"]
    assert "post" not in paths[USERS]
    for path, operations in paths.items():
        if path.startswith("/api/admin/"):
            assert "delete" not in operations


def test_employee_state_deactivation_and_reactivation(client, operator, employee, factory):
    """CU05 baja lógica: sin borrado físico, con auditoría de cada transición."""
    path = EMPLOYEES + "/" + employee["idUsuario"] + "/estado-cuenta"
    assert employee["estado"] == "activo"
    before = audit(factory)
    off = client.patch(path, json={"activo": False})
    assert off.status_code == 200 and off.json()["estado"] == "inactivo"
    assert client.get(USERS + "/" + employee["idUsuario"]).status_code == 200
    with factory() as db:
        assert db.get(Empleado, employee["idUsuario"]) is not None
        assert count(db, Empleado) == 1 and count(db, Usuario) == 2
    assert audit(factory)[len(before):] == [("usuario_desactivado", operator, {"resultado": "exito"})]
    on = client.patch(path, json={"activo": True})
    assert on.status_code == 200 and on.json()["estado"] == "activo"
    assert audit(factory)[len(before) + 1:] == [("usuario_activado", operator, {"resultado": "exito"})]


def test_inactive_employee_cannot_login_or_operate(client, app, operator, employee, factory, payload):
    """CU05: la baja lógica corta el acceso, incluso una sesión ya abierta."""
    login = {"correo": payload["correo"], "contrasena": payload["contrasena"]}
    with closing(TestClient(app)) as other:
        other.headers.update({"Origin": "http://localhost:4200", "X-CSRF-Protection": "1"})
        assert other.post("/api/auth/login", json=login).status_code == 200
        assert other.get("/api/auth/me").status_code == 200
        assert client.patch(EMPLOYEES + "/" + employee["idUsuario"] + "/estado-cuenta",
                            json={"activo": False}).status_code == 200
        assert other.get("/api/auth/me").status_code == 401
        other.cookies.clear()
        assert other.post("/api/auth/login", json=login).status_code == 401
    with factory() as db:
        assert db.get(Empleado, employee["idUsuario"]) is not None


def test_last_cu06_employee_cannot_be_deactivated(client, factory, operator, payload):
    """CU06 continuidad: un empleado inactivo no cuenta como usuario operativo."""
    first = client.post(EMPLOYEES, json=payload | {"nroRol": "roles", "correo": "uno@example.com"})
    second = client.post(EMPLOYEES, json=payload | {"nroRol": "roles", "correo": "dos@example.com"})
    assert first.status_code == 201 and second.status_code == 201
    path = EMPLOYEES + "/{}/estado-cuenta"
    assert client.patch(path.format(first.json()["idUsuario"]), json={"activo": False}).status_code == 200
    refused = client.patch(path.format(second.json()["idUsuario"]), json={"activo": False})
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "ultimo_usuario_cu06"
    with factory() as db:
        assert db.get(Empleado, first.json()["idUsuario"]).estado == "inactivo"
        assert db.get(Empleado, second.json()["idUsuario"]).estado == "activo"
