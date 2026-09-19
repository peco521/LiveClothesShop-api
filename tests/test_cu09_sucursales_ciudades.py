"""CU09: injected SQLite only; PostgreSQL contracts compiled without connections."""
from datetime import timedelta
from decimal import Decimal
import re
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateTable

from app.core.security import utcnow
from app.integrations.geocoding import AddressNotFound, GeocodingUnavailable
from app.modules.seguridad_accesos.models import Bitacora, Funcion, Rol, RolFuncion, Usuario
from app.modules.seguridad_accesos.shared.models import Ciudad, Sucursal
from app.modules.seguridad_accesos.shared.repositories import organizacion as repository
from app.modules.seguridad_accesos.cu05_usuarios_empleados.repositories import usuario as cu05
from app.modules.seguridad_accesos.cu05_usuarios_empleados.models import Empleado
from app.modules.seguridad_accesos.cu09_sucursales_ciudades.services import organizacion as service
from app.modules.seguridad_accesos.cu09_sucursales_ciudades.schemas.organizacion import SucursalesFiltros

CITIES = "/api/admin/ciudades"
BRANCHES = "/api/admin/sucursales"
ACTIONS = ["ciudad_creada", "ciudad_actualizada", "sucursal_creada", "sucursal_actualizada", "sucursal_estado_actualizado"]
ROUTES = [(method, base + suffix) for base in (CITIES, BRANCHES)
          for method, suffix in (("GET", ""), ("POST", ""), ("GET", "/0"), ("PATCH", "/0"))]


@pytest.fixture
def operator(client, registered, credentials, factory):
    with factory.begin() as db:
        db.add(Rol(nro="organizacion", descripcion="Operador CU09"))
        db.add(Funcion(id="CU09", descripcion="Gestionar Sucursales y Ciudades"))
        db.flush()
        db.add(RolFuncion(nrorol="organizacion", idfun="CU09", descripcion="Prueba"))
        db.get(Usuario, registered["idUsuario"]).nrorol = "organizacion"
    assert client.post("/api/auth/login", json=credentials).status_code == 200
    return registered["idUsuario"]


@pytest.fixture
def branch(client, operator, factory):
    with factory.begin() as db:
        db.add(Ciudad(id=0, nombre="Ciudad"))
        service.record(db, "ciudad_creada", operator, None, True)
    response = client.post(BRANCHES, json={"nombre": "Central", "direccion": "Calle", "idCiud": 0})
    assert response.status_code == 201
    return response.json()


def audit(factory):
    with factory() as db:
        return [(row.accion, row.usuario_id, row.detalles) for row in db.scalars(select(Bitacora).order_by(Bitacora.id))]


class FakeGeocoder:
    """Sustituto determinista del proveedor: sin red, sin claves."""

    def __init__(self, result=None, error=None):
        self.calls = []
        self._result = result
        self._error = error

    def locate(self, direccion, ciudad):
        self.calls.append((direccion, ciudad))
        if self._error is not None:
            raise self._error
        return self._result


@pytest.fixture
def geocoder(app):
    fake = FakeGeocoder(result=(Decimal("-17.783333"), Decimal("-63.182222")))
    app.state.geocoder = fake
    return fake


def branch_in_db(factory, **values):
    with factory.begin() as db:
        row = Sucursal(nombre="Antigua", direccion="Calle vieja 1", estado="activo", idciud=0, **values)
        db.add(row)
        db.flush()
        return row.nro


def test_branch_creation_stores_verified_coordinates(client, operator, factory, app, geocoder):
    with factory.begin() as db:
        db.add(Ciudad(id=0, nombre="Santa Cruz"))
    response = client.post(BRANCHES, json={"nombre": "Central", "direccion": "Calle Real 10", "idCiud": 0})
    assert response.status_code == 201, response.text
    assert geocoder.calls == [("Calle Real 10", "Santa Cruz")]
    body = response.json()
    assert Decimal(str(body["latitud"])) == Decimal("-17.783333")
    assert Decimal(str(body["longitud"])) == Decimal("-63.182222")
    with factory() as db:
        row = db.get(Sucursal, body["nro"])
        assert Decimal(str(row.latitud)) == Decimal("-17.783333")


def test_branch_creation_rejects_unknown_address(client, operator, factory, app):
    with factory.begin() as db:
        db.add(Ciudad(id=0, nombre="Santa Cruz"))
    app.state.geocoder = FakeGeocoder(error=AddressNotFound())
    before = audit(factory)
    response = client.post(BRANCHES, json={"nombre": "Central", "direccion": "Calle inexistente 999", "idCiud": 0})
    assert response.status_code == 422 and response.json()["error"]["code"] == "direccion_no_verificada"
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Sucursal)) == 0
    assert audit(factory) == before


def test_branch_provider_failure_is_not_address_not_found(client, operator, factory, app):
    with factory.begin() as db:
        db.add(Ciudad(id=0, nombre="Santa Cruz"))
    for error in (GeocodingUnavailable(), TimeoutError("timeout")):
        app.state.geocoder = FakeGeocoder(error=error)
        response = client.post(BRANCHES, json={"nombre": "Central", "direccion": "Calle Real 10", "idCiud": 0})
        assert response.status_code == 503 and response.json()["error"]["code"] == "geocodificacion_no_disponible"
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Sucursal)) == 0


def test_legacy_null_coordinates_are_tolerated_and_edit_validates(client, operator, factory, app, geocoder):
    with factory.begin() as db:
        db.add(Ciudad(id=0, nombre="Santa Cruz"))
    nro = branch_in_db(factory)
    detail = client.get(BRANCHES + f"/{nro}")
    assert detail.status_code == 200 and detail.json()["latitud"] is None and detail.json()["longitud"] is None
    edited = client.patch(BRANCHES + f"/{nro}", json={"direccion": "Calle Nueva 20"})
    assert edited.status_code == 200, edited.text
    assert geocoder.calls == [("Calle Nueva 20", "Santa Cruz")]
    assert Decimal(str(edited.json()["latitud"])) == Decimal("-17.783333")


def test_edit_without_address_change_keeps_coordinates(client, operator, factory, app, geocoder):
    with factory.begin() as db:
        db.add(Ciudad(id=0, nombre="Santa Cruz"))
    created = client.post(BRANCHES, json={"nombre": "Central", "direccion": "Calle Real 10", "idCiud": 0})
    assert created.status_code == 201
    geocoder.calls.clear()
    edited = client.patch(BRANCHES + f"/{created.json()['nro']}", json={"nombre": "Renombrada"})
    assert edited.status_code == 200 and geocoder.calls == []
    assert Decimal(str(edited.json()["latitud"])) == Decimal("-17.783333")


@pytest.mark.parametrize("method,path", ROUTES)
def test_01_requires_auth(client, method, path):
    assert client.request(method, path).status_code == 401


@pytest.mark.parametrize("method,path", ROUTES)
def test_02_requires_permission(client, registered, credentials, method, path):
    client.post("/api/auth/login", json=credentials)
    assert client.request(method, path).status_code == 403


@pytest.mark.parametrize("permission", [None, "CU05", "CU06", "CU07", "CU08"])
def test_03_no_bypass(client, operator, factory, permission):
    with factory.begin() as db:
        db.add(Rol(nro="superadmin", descripcion="SuperAdmin"))
        db.flush()
        db.get(Usuario, operator).nrorol = "superadmin"
        if permission:
            db.add(Funcion(id=permission, descripcion="Prueba"))
            db.flush()
            db.add(RolFuncion(nrorol="superadmin", idfun=permission, descripcion="Prueba"))
    for method, path in ROUTES:
        assert client.request(method, path).status_code == 403


@pytest.mark.parametrize("state", ["password_changed", "expired", "revoked", "permission"])
def test_04_access_revocation(client, operator, factory, state, settings, monkeypatch):
    if state == "password_changed":
        with factory.begin() as db:
            db.get(Usuario, operator).contrasena = "changed-password-hash"
    elif state == "permission":
        with factory.begin() as db:
            db.delete(db.get(RolFuncion, ("organizacion", "CU09")))
    elif state == "expired":
        monkeypatch.setattr("app.core.access_tokens.utcnow", lambda: utcnow() + timedelta(hours=9))
    else:
        client.app.state.access_tokens.revoke(client.cookies.get(settings.cookie_name))
    for method, path in ROUTES:
        assert client.request(method, path).status_code == (403 if state == "permission" else 401)


@pytest.mark.parametrize("id", [-32768, -1, 0, 32767])
def test_05_city_manual_full_range(client, operator, id, factory):
    with factory.begin() as db:
        db.add(Ciudad(id=id, nombre="Ciudad"))
    response = client.get(f"{CITIES}/{id}")
    assert response.status_code == 200 and response.json() == {"id": id, "nombre": "Ciudad"}
    assert client.get(f"{CITIES}/{id}").json() == response.json()
    assert client.patch(f"{CITIES}/{id}", json={"nombre": "Nueva"}).json()["id"] == id


@pytest.mark.parametrize("id", [True, False, "1", 1.0, 1.5, None, -32769, 32768])
def test_06_strict_city_json_id(client, operator, id):
    assert client.post(CITIES, json={"id": id, "nombre": "Ciudad"}).status_code == 422


def test_07_duplicate_names_not_ids(client, operator, factory):
    for id in (0, 1):
        assert client.post(CITIES, json={"nombre": "Igual"}).status_code == 201
    before = audit(factory)
    response = client.post(CITIES, json={"id": 0, "nombre": "Otra"})
    assert response.status_code == 422
    assert audit(factory) == before


@pytest.mark.parametrize("payload", [{}, {"nombre": ""}, {"nombre": " "},
    {"nombre": "x" * 51}, {"nombre": None}, {"nombre": 1},
    {"nombre": "Ciudad", "estado": "activo"}])
def test_08_city_validation(client, operator, payload):
    assert client.post(CITIES, json=payload).status_code == 422


@pytest.mark.parametrize("payload", [{}, {"nombre": None}, {"id": 1}, {"nombre": " "}, {"nombre": "x" * 51}])
def test_09_city_patch_allowlist(client, branch, factory, payload):
    before = audit(factory)
    assert client.patch(CITIES + "/0", json=payload).status_code == 422
    assert client.get(CITIES + "/0").json()["nombre"] == "Ciudad"
    assert audit(factory) == before


def test_10_branch_generation_defaults_duplicates(client, branch, operator, factory):
    second = client.post(BRANCHES, json={"nombre": branch["nombre"], "direccion": branch["direccion"], "idCiud": 0})
    assert second.status_code == 201 and second.json()["nro"] != branch["nro"]
    assert branch["estado"] == "activo" and branch["ciudad"] == {"id": 0, "nombre": "Ciudad"}
    assert audit(factory)[-1] == ("sucursal_creada", operator, {"resultado": "exito"})


@pytest.mark.parametrize("key,value", [("nro", 9), ("idCiud", True), ("idCiud", "0"), ("idCiud", 0.0),
    ("idCiud", -32769), ("idCiud", 32768), ("idCiud", None), ("nombre", "x" * 51),
    ("nombre", " "), ("direccion", "x" * 101), ("direccion", ""), ("estado", "ACTIVO"),
    ("estado", "eliminado"), ("estado", None), ("activo", False)])
def test_11_branch_create_patch_validation(client, branch, key, value):
    payload = {"nombre": "Nueva", "direccion": "Calle", "idCiud": 0}
    assert client.post(BRANCHES, json=payload | {key: value}).status_code == 422
    assert client.patch(f"{BRANCHES}/{branch['nro']}", json={key: value}).status_code == 422


def test_12_reference_validation_and_patch_empty(client, branch, factory):
    before = audit(factory)
    assert client.post(BRANCHES, json={"nombre": "Nueva", "direccion": "Calle", "idCiud": 999}).status_code == 422
    assert client.patch(f"{BRANCHES}/{branch['nro']}", json={"idCiud": 999}).status_code == 422
    assert client.patch(f"{BRANCHES}/{branch['nro']}", json={}).status_code == 422
    assert audit(factory) == before


def test_13_max_lengths_and_negative_city_reassignment(client, branch, factory):
    with factory.begin() as db:
        db.add(Ciudad(id=-32768, nombre="x" * 50))
    path = f"{BRANCHES}/{branch['nro']}"
    response = client.patch(path, json={"nombre": "x" * 50, "direccion": "x" * 100, "idCiud": -32768})
    assert response.status_code == 200 and response.json()["idCiud"] == -32768
    assert response.json()["ciudad"]["nombre"] == "x" * 50
    assert client.get(path).json() == response.json()
    with factory.begin() as db:
        db.add(Ciudad(id=32767, nombre="Extremo"))
    assert client.post(BRANCHES, json={"nombre": "N", "direccion": "D", "estado": "inactivo", "idCiud": 32767}).status_code == 201


@pytest.mark.parametrize("base,id,status", [(CITIES, -32769, 422), (CITIES, 32768, 422), (CITIES, 32767, 404),
    (BRANCHES, -2147483649, 422), (BRANCHES, 2147483648, 422), (BRANCHES, 2147483647, 404),
    (CITIES, "abc", 422), (BRANCHES, "abc", 422)])
def test_14_path_ranges_missing(client, operator, base, id, status):
    assert client.get(f"{base}/{id}").status_code == status
    assert client.patch(f"{base}/{id}", json={"nombre": "Nuevo"}).status_code == status


@pytest.mark.parametrize("nro", [-2147483648, -1, 0, 2147483647])
def test_15_existing_integer_ids_not_positive_only(client, branch, factory, nro):
    with factory.begin() as db:
        db.add(Sucursal(nro=nro, nombre="Legacy", direccion="Calle", idciud=0))
    assert client.get(f"{BRANCHES}/{nro}").status_code == 200
    assert client.patch(f"{BRANCHES}/{nro}", json={"estado": "inactivo"}).status_code == 200


def test_16_city_pagination_literal_search(client, operator, factory):
    for id, name in [(2, "Igual"), (1, "Igual"), (0, "A%_/B"), (-1, "Otro")]:
        with factory.begin() as db:
            db.add(Ciudad(id=id, nombre=name))
    result = client.get(CITIES, params={"q": " IGUAL ", "offset": 1, "limit": 1}).json()
    assert result == {"items": [{"id": 2, "nombre": "Igual"}], "total": 2, "offset": 1, "limit": 1}
    for q in ("%", "_", "/", "a%_/b"):
        assert client.get(CITIES, params={"q": q}).json()["items"] == [{"id": 0, "nombre": "A%_/B"}]
    assert client.get(CITIES, params={"q": "' OR 1=1 --"}).json()["total"] == 0
    assert client.get(CITIES, params={"offset": 999}).json()["items"] == []


def test_17_branch_filters_order_total(client, branch, factory):
    with factory.begin() as db:
        db.add(Ciudad(id=-1, nombre="Antes"))
    for city_id, state in [(0, "activo"), (0, "inactivo"), (-1, "activo")]:
        assert client.post(BRANCHES, json={"nombre": "Igual", "direccion": "A%_/B", "idCiud": city_id, "estado": state}).status_code == 201
    filters = {"q": " / ", "idCiud": 0, "estado": "activo", "limit": 1}
    result = client.get(BRANCHES, params=filters).json()
    assert result["total"] == 1 and result["items"][0]["idCiud"] == 0
    assert client.get(BRANCHES, params=filters | {"offset": 1}).json()["items"] == []
    assert client.get(BRANCHES, params={"q": "IGUAL"}).json()["total"] == 3
    for q in ("%", "_", "/", "a%_/b"):
        rows = client.get(BRANCHES, params={"q": q}).json()["items"]
        assert len(rows) == 3 and rows[0]["idCiud"] == -1 and rows[1]["nro"] < rows[2]["nro"]
    assert client.get(BRANCHES, params={"idCiud": 999}).json()["total"] == 0
    assert client.get(BRANCHES, params={"estado": "inactivo"}).json()["total"] == 1


@pytest.mark.parametrize("base", [CITIES, BRANCHES])
@pytest.mark.parametrize("params", [{"offset": -1}, {"offset": 9223372036854775808}, {"offset": "1.5"},
    {"limit": 0}, {"limit": 101}, {"limit": "1.5"}, {"q": "x" * 101}, {"unknown": "x"}])
def test_18_invalid_pagination(client, operator, base, params):
    assert client.get(base, params=params).status_code == 422


@pytest.mark.parametrize("params", [{"estado": "Activo"}, {"estado": ""}, {"idCiud": 32768}, {"idCiud": -32769}, {"idCiud": "true"}])
def test_19_invalid_branch_filters(client, operator, params):
    assert client.get(BRANCHES, params=params).status_code == 422


def test_20_exact_audit_noop_and_combined(client, branch, factory, operator):
    path = f"{BRANCHES}/{branch['nro']}"
    before = audit(factory)
    assert client.patch(CITIES + "/0", json={"nombre": " Ciudad "}).status_code == 200
    assert client.patch(path, json={"nombre": " Central ", "direccion": "Calle", "estado": "activo", "idCiud": 0}).status_code == 200
    assert audit(factory) == before
    for data, expected in [({"nombre": "Nueva"}, ["sucursal_actualizada"]),
                           ({"estado": "inactivo"}, ["sucursal_estado_actualizado"]),
                           ({"direccion": "Otra", "estado": "activo"}, ["sucursal_actualizada", "sucursal_estado_actualizado"])]:
        start = len(audit(factory))
        assert client.patch(path, json=data).status_code == 200
        assert audit(factory)[start:] == [(action, operator, {"resultado": "exito"}) for action in expected]
    assert client.patch(CITIES + "/0", json={"nombre": "Otra"}).status_code == 200
    assert audit(factory)[-1] == ("ciudad_actualizada", operator, {"resultado": "exito"})


@pytest.mark.parametrize("operation", ["city_create", "branch_create", "city_edit", "branch_edit", "second_audit"])
def test_21_atomic_rollback(client, branch, factory, monkeypatch, operation, caplog):
    before = audit(factory)
    original = service.record
    def fail(db, action, *args):
        if operation != "second_audit" or action == "sucursal_estado_actualizado":
            raise RuntimeError("synthetic-private-marker")
        original(db, action, *args)
    monkeypatch.setattr(service, "record", fail)
    path = f"{BRANCHES}/{branch['nro']}"
    if operation == "city_create":
        response = client.post(CITIES, json={"nombre": "Nueva"})
        assert client.get(CITIES + "/1").status_code == 404
    elif operation == "branch_create":
        response = client.post(BRANCHES, json={"nombre": "Nueva", "direccion": "Otra", "idCiud": 0})
        assert client.get(BRANCHES).json()["total"] == 1
    elif operation == "city_edit":
        response = client.patch(CITIES + "/0", json={"nombre": "Nueva"})
    else:
        response = client.patch(path, json={"nombre": "Nueva", "estado": "inactivo"})
    assert response.status_code == 500
    assert "synthetic-private-marker" not in response.text + caplog.text
    assert client.get(path).json() == branch
    assert audit(factory) == before


def test_22_integrity_race_sanitized_rollback(client, operator, factory, monkeypatch):
    original = repository.add
    def conflict(db, row):
        original(db, row)
        raise IntegrityError("private-sql", {}, Exception("private-params"))
    monkeypatch.setattr(repository, "add", conflict)
    before = audit(factory)
    response = client.post(CITIES, json={"nombre": "Ciudad"})
    assert response.status_code == 409 and "private" not in response.text
    assert client.get(CITIES).json()["total"] == 0 and audit(factory) == before


@pytest.mark.parametrize("kind", ["missing", "permission"])
def test_23_revalidate_after_lock(client, branch, factory, monkeypatch, kind):
    original = repository.branch
    locked = []
    def lock(*args, **kwargs):
        locked.append(kwargs.get("lock"))
        return original(*args, **kwargs)
    monkeypatch.setattr(repository, "branch", lock)
    def actor(*args):
        assert locked == [True]
        return None if kind == "missing" else SimpleNamespace(nrorol="revoked")
    monkeypatch.setattr(service.continuidad, "actor", actor)
    before = audit(factory)
    assert client.patch(f"{BRANCHES}/{branch['nro']}", json={"nombre": "Otra"}).status_code == (401 if kind == "missing" else 403)
    assert audit(factory) == before


def test_24_shared_cu05_compatibility(client, branch, operator, factory):
    assert cu05.branch is repository.branch and cu05.branches is repository.branches and cu05.cities is repository.cities
    assert client.get("/api/admin/empleados/ciudades").status_code == 403
    assert client.get("/api/admin/bitacora").status_code == 403
    client.patch(f"{BRANCHES}/{branch['nro']}", json={"estado": "inactivo"})
    with factory.begin() as db:
        db.add(Funcion(id="CU05", descripcion="Prueba"))
        db.flush()
        db.add(RolFuncion(nrorol="organizacion", idfun="CU05", descripcion="Prueba"))
    assert client.get("/api/admin/empleados/ciudades").json() == [{"id": 0, "nombre": "Ciudad"}]
    options = client.get("/api/admin/empleados/sucursales", params={"idCiud": 0}).json()
    assert options[0]["estado"] == "inactivo"
    with factory() as db:
        from app.modules.seguridad_accesos.cu05_usuarios_empleados.services.usuario import validate_branch
        validate_branch(db, branch["nro"])


def test_25_cu08_recognizes_real_events(client, branch, factory):
    client.patch(CITIES + "/0", json={"nombre": "Otra"})
    client.patch(f"{BRANCHES}/{branch['nro']}", json={"nombre": "Nueva", "estado": "inactivo"})
    with factory.begin() as db:
        db.add(Funcion(id="CU08", descripcion="Prueba"))
        db.flush()
        db.add(RolFuncion(nrorol="organizacion", idfun="CU08", descripcion="Prueba"))
    before = audit(factory)
    for action in ACTIONS:
        response = client.get("/api/admin/bitacora", params={"accion": action})
        assert response.status_code == 200 and response.json()["total"] == 1
        item = response.json()["items"][0]
        assert item["accion"] == action
        detail = client.get("/api/admin/bitacora/" + item["id"]).json()
        assert detail["detalles"] == {"resultado": "exito"}
    assert audit(factory) == before


@pytest.mark.parametrize("base", [CITIES, BRANCHES])
def test_26_http_security(client, branch, base):
    assert client.post(base, json={}, headers={"Origin": "https://evil.example"}).status_code == 403
    client.headers.pop("X-CSRF-Protection")
    assert client.post(base, json={}).status_code == 403
    assert client.patch(base + "/0", json={"nombre": "Otra"}).status_code == 403
    for suffix in ("", "/9999", "?limit=0"):
        response = client.get(base + suffix)
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Pragma"] == "no-cache" and response.headers["X-Content-Type-Options"] == "nosniff"
    response = client.options(base, headers={"Access-Control-Request-Method": "PATCH"})
    assert response.status_code == 204 and response.headers["Access-Control-Allow-Methods"] == "GET, POST, PATCH, OPTIONS"
    assert response.headers["Access-Control-Allow-Credentials"] == "true"
    assert client.options(base, headers={"Origin": "https://evil.example"}).status_code == 403


def test_27_exact_routes_no_delete(app, client, branch):
    paths = app.openapi()["paths"]
    assert {p: set(v) for p, v in paths.items() if p.startswith((CITIES, BRANCHES))} == {
        CITIES: {"get", "post"}, CITIES + "/{id}": {"get", "patch"},
        BRANCHES: {"get", "post"}, BRANCHES + "/{nro}": {"get", "patch"},
        BRANCHES + "/horarios-sugeridos": {"get"}}
    for base in (CITIES, BRANCHES):
        for method in ("DELETE", "PUT"):
            assert client.request(method, base + "/0").status_code == 405


def test_28_read_no_dml(client, branch, factory):
    statements = []
    def capture(conn, cursor, statement, *args):
        statements.append(statement)
    engine = factory.kw["bind"]
    event.listen(engine, "before_cursor_execute", capture)
    try:
        for path in (CITIES, CITIES + "/0", BRANCHES, f"{BRANCHES}/{branch['nro']}"):
            assert client.get(path).status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert statements and all(sql.lstrip().upper().startswith("SELECT") for sql in statements)


def test_29_dependents_unchanged(client, branch, factory, operator):
    # The isolated fixture already creates all mapped business tables (CU10–CU14).
    # This regression must not depend on an untracked external schema.sql file.
    with factory.begin() as db:
        actor = db.get(Usuario, operator)
        employee_data = {column.key: getattr(actor, column.key) for column in Usuario.__table__.columns}
        employee_data.update(idusuario="employee-fixture", correo="employee@example.com", tipo="E")
        db.add(Usuario(**employee_data))
        db.flush()
        db.add(Empleado(idusuario="employee-fixture", cod_emp="EMP", cargo="Prueba", nrosuc=branch["nro"]))
        db.flush()
        db.execute(text("INSERT INTO talla VALUES (1, 'M')"))
        db.execute(text("INSERT INTO categoria VALUES (1, 'Prueba')"))
        db.execute(text("INSERT INTO marca VALUES (1, 'Prueba', 'activo')"))
        db.execute(text("INSERT INTO coleccion VALUES (1, 'Prueba')"))
        db.execute(text("INSERT INTO proveedor VALUES (1, 'Prueba', 'test@example.com', 'Prueba', '0')"))
        db.execute(text("INSERT INTO producto VALUES ('p', 'Prueba', 'activo', NULL, 1, 1, 1, 1)"))
        db.execute(text("INSERT INTO varianteProd VALUES ('v', 'sku', 1, 'activo', NULL, 1, 'p')"))
        db.execute(text("INSERT INTO horario_atencion VALUES (1, '08:00', '18:00')"))
        db.execute(text("INSERT INTO horario_suc (idaten,nrosuc) VALUES (1, :nro)"), {"nro": branch["nro"]})
        db.execute(text("INSERT INTO inventario VALUES (1, 10, 8, :nro, 'v')"), {"nro": branch["nro"]})
        db.execute(text("INSERT INTO reserva VALUES (1, '2026-09-12', '10:00', 'pendiente', :nro, :actor)"), {"nro": branch["nro"], "actor": operator})
        db.execute(text("INSERT INTO venta VALUES (1, NULL, '2026-09-12 10:00', 1, 0, 'registrada', :actor, NULL, :nro, NULL, 1)"), {"actor": operator, "nro": branch["nro"]})
    tables = ["empleado", "horario_atencion", "horario_suc", "inventario", "reserva", "venta", "usuario", "rol", "funcion", "rol_funcion"]
    def snapshot():
        with factory() as db:
            return {name: db.execute(text("SELECT * FROM " + name)).all() for name in tables}
    before = snapshot()
    statements = []
    def capture(conn, cursor, statement, *args):
        statements.append(statement)
    engine = factory.kw["bind"]
    event.listen(engine, "before_cursor_execute", capture)
    try:
        assert client.patch(CITIES + "/0", json={"nombre": "Renombrada"}).status_code == 200
        detail = client.get(f"{BRANCHES}/{branch['nro']}").json()
        assert detail["idCiud"] == 0 and detail["ciudad"]["nombre"] == "Renombrada"
        nueva = client.post(CITIES, json={"nombre": "Destino"})
        assert nueva.status_code == 201
        assert client.patch(f"{BRANCHES}/{branch['nro']}", json={"estado": "inactivo", "idCiud": nueva.json()["id"]}).status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert snapshot() == before
    dml = [statement for statement in statements if re.match(r"\s*(INSERT|UPDATE|DELETE)", statement, re.I)]
    assert dml and all(re.match(r"\s*(?:INSERT INTO|UPDATE) (?:ciudad|sucursal|bitacora)\b", statement, re.I) for statement in dml)


def test_30_postgresql_mapping_and_parameterized_queries():
    city_ddl = str(CreateTable(Ciudad.__table__).compile(dialect=postgresql.dialect()))
    branch_ddl = str(CreateTable(Sucursal.__table__).compile(dialect=postgresql.dialect()))
    assert "id SMALLINT GENERATED BY DEFAULT AS IDENTITY" in city_ddl and "UNIQUE" not in city_ddl
    assert "nro SERIAL NOT NULL" in branch_ddl and "nombre VARCHAR(50) NOT NULL" in branch_ddl
    assert "direccion VARCHAR(100) NOT NULL" in branch_ddl and "DEFAULT 'activo'" in branch_ddl
    assert "ON DELETE RESTRICT ON UPDATE CASCADE" in branch_ddl and "UNIQUE" not in branch_ddl
    filters = SucursalesFiltros(q="' OR 1=1 --", idCiud=0, estado="inactivo")
    compiled = select(Sucursal).where(*repository.predicates(filters, branches=True)).compile(dialect=postgresql.dialect())
    assert "OR 1=1" not in str(compiled) and "' or 1=1 --" in compiled.params.values()


def test_31_locks_refresh_without_global_lock():
    db = Mock()
    repository.branch(db, 0)
    query = db.scalar.call_args.args[0]
    assert "FOR UPDATE" in str(query.compile(dialect=postgresql.dialect()))
    assert query.get_execution_options()["populate_existing"] is True
    repository.city(db, 0, lock=True)
    query = db.scalar.call_args.args[0]
    assert "FOR UPDATE" in str(query.compile(dialect=postgresql.dialect()))
    assert query.get_execution_options()["populate_existing"] is True
    db.execute.assert_not_called()


def test_32_defaults_empty_and_noop_no_dml(client, operator, factory):
    for base in (CITIES, BRANCHES):
        assert client.get(base).json() == {"items": [], "total": 0, "offset": 0, "limit": 20}
        assert client.get(base, params={"limit": 100, "offset": 9223372036854775807}).status_code == 200
    with factory.begin() as db:
        db.add(Ciudad(id=0, nombre="Ciudad"))
    statements = []
    def capture(conn, cursor, statement, *args):
        statements.append(statement)
    engine = factory.kw["bind"]
    event.listen(engine, "before_cursor_execute", capture)
    try:
        assert client.patch(CITIES + "/0", json={"nombre": "Ciudad"}).status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)


def test_33_cu09_repository_reuses_shared_implementation():
    from app.modules.seguridad_accesos.cu09_sucursales_ciudades.repositories import organizacion
    assert organizacion is repository and service.repository is repository


def test_34_create_edit_shared_hours(client, branch, operator, factory):
    ranges = [{"horaIni": "08:00:00", "horaFin": "12:00:00", "dias": [1,2,3,4,5,6,7]}, {"horaIni": "14:00:00", "horaFin": "18:00:00", "dias": [1,2,3,4,5,6,7]}]
    data = {"nombre": "Otra sucursal", "direccion": "Calle", "idCiud": 0, "horarios": ranges}
    first = client.post(BRANCHES, json=data)
    second = client.post(BRANCHES, json=data)
    assert first.status_code == second.status_code == 201
    assert first.json()["horarios"] == ranges
    path = f"{BRANCHES}/{first.json()['nro']}"
    changed = [{"horaIni": "09:00:00", "horaFin": "17:00:00", "dias": [1,2,3,4,5,6,7]}]
    assert client.patch(path, json={"horarios": changed}).json()["horarios"] == changed
    assert client.get(f"{BRANCHES}/{second.json()['nro']}").json()["horarios"] == ranges
    assert client.get(BRANCHES).json()["items"]
    before = audit(factory)
    assert client.patch(path, json={"horarios": changed}).status_code == 200
    assert audit(factory) == before
    from datetime import time
    from app.modules.cliente_experiencia_compra.cu11_gestionar_reserva.services.reserva import _validar_horario
    from app.core.errors import DomainError
    with factory() as db:
        assert _validar_horario(db, first.json()["nro"], time(10))
        with pytest.raises(DomainError):
            _validar_horario(db, first.json()["nro"], time(18))
    assert client.patch(path, json={"horarios": []}).json()["horarios"] == []
    assert client.get(f"{BRANCHES}/{second.json()['nro']}").json()["horarios"] == ranges


@pytest.mark.parametrize("ranges", [
    [{"horaIni": "18:00", "horaFin": "08:00"}],
    [{"horaIni": "08:00", "horaFin": "08:00"}],
    [{"horaIni": "25:00", "horaFin": "26:00"}],
    [{"horaIni": "08:00Z", "horaFin": "18:00Z"}],
    [{"horaIni": "08:00", "horaFin": "12:00"}, {"horaIni": "11:00", "horaFin": "18:00"}],
])
def test_35_invalid_hours_no_changes(client, branch, ranges, factory):
    before = audit(factory)
    path = f"{BRANCHES}/{branch['nro']}"
    assert client.patch(path, json={"horarios": ranges}).status_code == 422
    assert client.post(BRANCHES, json={"nombre": "Nueva", "direccion": "Calle", "idCiud": 0, "horarios": ranges}).status_code == 422
    assert client.get(path).json() == branch
    assert audit(factory) == before


def test_36_hours_rollback_with_audit(client, branch, factory, monkeypatch):
    def fail(*args):
        raise RuntimeError("private-marker")
    monkeypatch.setattr(service, "record", fail)
    path = f"{BRANCHES}/{branch['nro']}"
    response = client.patch(path, json={"horarios": [{"horaIni": "08:00", "horaFin": "18:00"}]})
    assert response.status_code == 500
    assert "private-marker" not in response.text
    assert client.get(path).json()["horarios"] == []
    with factory() as db:
        assert db.execute(text("SELECT * FROM horario_atencion")).all() == []
        assert db.execute(text("SELECT * FROM horario_suc")).all() == []


def test_37_days_are_branch_specific_and_suggestions_reuse_hours(client, branch, factory):
    from datetime import date, time
    from app.core.errors import DomainError
    from app.modules.cliente_experiencia_compra.cu11_gestionar_reserva.services.reserva import _validar_horario
    one = [{"horaIni": "08:00:00", "horaFin": "18:00:00", "dias": [1, 3, 5]}]
    two = [{"horaIni": "08:00:00", "horaFin": "18:00:00", "dias": [2, 4]}]
    path = f"{BRANCHES}/{branch['nro']}"
    assert client.patch(path, json={"horarios": one}).json()["horarios"] == one
    other = client.post(BRANCHES, json={"nombre": "Otra", "direccion": "Calle", "idCiud": 0, "horarios": two})
    assert other.status_code == 201 and other.json()["horarios"] == two
    suggestions = client.get(BRANCHES + "/horarios-sugeridos").json()
    assert len(suggestions) == 1 and suggestions[0]["horaIni"] == "08:00:00"
    assert client.get(path).json()["horarios"] == one
    with factory() as db:
        assert len(db.execute(text("SELECT * FROM horario_atencion")).all()) == 1
        assert db.execute(text("SELECT dias FROM horario_suc WHERE nrosuc = :nro"), {"nro": branch["nro"]}).scalar() == "Lunes,Miercoles,Viernes"
        assert _validar_horario(db, branch["nro"], time(10), date(2026, 9, 14))
        with pytest.raises(DomainError):
            _validar_horario(db, branch["nro"], time(10), date(2026, 9, 15))
    different_days = [{"horaIni": "09:00", "horaFin": "12:00", "dias": [1]}, {"horaIni": "10:00", "horaFin": "13:00", "dias": [2]}]
    assert client.patch(path, json={"horarios": different_days}).status_code == 200
    different_days[1]["dias"] = [1]
    assert client.patch(path, json={"horarios": different_days}).status_code == 422
    assert client.patch(path, json={"horarios": [{"horaIni": "08:00", "horaFin": "18:00", "dias": []}]}).status_code == 422
