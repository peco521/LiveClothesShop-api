"""CU08: synthetic fixtures only; no provisioning or external database."""
from datetime import datetime, timedelta, timezone
from ipaddress import ip_address, ip_network
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import event, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.core.errors import DomainError
from app.core.security import utcnow
from app.modules.seguridad_accesos.models import Bitacora, Funcion, Rol, RolFuncion, Usuario
from app.modules.seguridad_accesos.cu08_bitacora.repositories import bitacora as repository
from app.modules.seguridad_accesos.cu08_bitacora.schemas.bitacora import BitacoraFiltros, BitacoraResumen
from app.modules.seguridad_accesos.cu08_bitacora.services import bitacora as service
from app.modules.seguridad_accesos.services import bitacora as writer

BASE = "/api/admin/bitacora"
INSTANT = datetime(2026, 9, 12, 12, 30, 45, 123456, tzinfo=timezone.utc)
ACTIONS = [
    "cliente_registrado", "login_correcto", "login_rechazado", "logout_correcto",
    "recuperacion_solicitada", "contrasena_restablecida", "usuario_creado", "empleado_creado",
    "usuario_actualizado", "empleado_actualizado", "usuario_activado", "usuario_desactivado",
    "rol_creado", "rol_actualizado", "permisos_rol_actualizados", "rol_activado", "rol_desactivado",
    "cliente_actualizado", "cliente_activado", "cliente_desactivado",
    "ciudad_creada", "ciudad_actualizada", "sucursal_creada", "sucursal_actualizada", "sucursal_estado_actualizado",
    "reserva_creada", "reserva_cancelada", "reserva_vencida",
    "carrito_item_agregado", "carrito_item_actualizado", "carrito_item_eliminado",
    "venta_registrada", "pago_iniciado", "pago_aprobado", "pago_rechazado", "venta_anulada",
    "catalogo_guardado", "catalogo_eliminado", "proveedor_guardado", "proveedor_eliminado",
    "inventario_movimiento_registrado",
]


@pytest.fixture
def operator(client, registered, credentials, factory):
    with factory.begin() as db:
        db.add(Rol(nro="auditor", descripcion="Operador de prueba"))
        db.add(Funcion(id="CU08", descripcion="Consultar bitácora"))
        db.flush()
        db.add(RolFuncion(nrorol="auditor", idfun="CU08", descripcion="Prueba"))
        db.get(Usuario, registered["idUsuario"]).nrorol = "auditor"
    assert client.post("/api/auth/login", json=credentials).status_code == 200
    return registered["idUsuario"]


def seed(factory, **changes):
    values = dict(accion="rol_creado", usuario_id=None, ip="2001:0db8:0:0:0:0:0:1",
                  detalles={"resultado": "exito", "rol": "synthetic-secret"}, fecha=INSTANT)
    with factory.begin() as db:
        row = Bitacora(**(values | changes))
        db.add(row)
        db.flush()
        return str(row.id)


@pytest.mark.parametrize("path", [BASE, BASE + "/1"])
def test_01_authentication_required(client, path):
    assert client.get(path).status_code == 401


@pytest.mark.parametrize("path", [BASE, BASE + "/1"])
def test_02_permission_required(client, registered, credentials, path):
    client.post("/api/auth/login", json=credentials)
    assert client.get(path).status_code == 403


@pytest.mark.parametrize("permission", [None, "CU05", "CU06", "CU07", "CU09"])
def test_03_no_role_name_or_other_permission_bypass(client, operator, factory, permission):
    with factory.begin() as db:
        db.add(Rol(nro="superadmin", descripcion="SuperAdmin"))
        db.flush()
        db.get(Usuario, operator).nrorol = "superadmin"
        if permission:
            db.add(Funcion(id=permission, descripcion="Prueba"))
            db.flush()
            db.add(RolFuncion(nrorol="superadmin", idfun=permission, descripcion="Prueba"))
    assert client.get(BASE).status_code == 403
    assert client.get(BASE + "/1").status_code == 403


@pytest.mark.parametrize("state", ["expired", "revoked", "password_changed"])
def test_04_invalid_sessions(client, operator, factory, state, settings, monkeypatch):
    if state == "password_changed":
        with factory.begin() as db:
            db.get(Usuario, operator).contrasena = "changed-password-hash"
    elif state == "expired":
        monkeypatch.setattr("app.core.access_tokens.utcnow", lambda: utcnow() + timedelta(hours=9))
    else:
        client.app.state.access_tokens.revoke(client.cookies.get(settings.cookie_name))
    for path in (BASE, BASE + "/1"):
        assert client.get(path).status_code == 401


def test_05_summary_detail_contract_and_microseconds(client, operator, factory):
    id = seed(factory, usuario_id=operator)
    response = client.get(BASE + "/" + id)
    assert response.status_code == 200
    assert response.json() == dict(id=id, usuario_id=operator, accion="rol_creado",
                                  fecha="2026-09-12T12:30:45.123456Z", ip="2001:db8::1",
                                  detalles={"resultado": "exito"})
    listed = client.get(BASE, params={"accion": "rol_creado"}).json()
    assert listed == dict(items=[{k: response.json()[k] for k in ("id", "usuario_id", "accion", "fecha", "ip")}],
                          total=1, offset=0, limit=20)


@pytest.mark.parametrize("ip,expected", [(None, None), ("synthetic-secret", None), ("127.0.0.1", "127.0.0.1")])
def test_list_includes_only_safe_ip(client, operator, factory, ip, expected):
    event_id = seed(factory, ip=ip)
    result = client.get(BASE, params={"accion": "rol_creado"})
    assert result.status_code == 200
    assert result.json()["items"][0]["id"] == event_id
    assert result.json()["items"][0]["ip"] == expected


@pytest.mark.parametrize("action", ACTIONS)
def test_06_all_exact_actions(client, operator, factory, action):
    result = "rechazado" if action == "login_rechazado" else "exito"
    id = seed(factory, accion=action, detalles={"resultado": result})
    value = client.get(BASE + "/" + id).json()
    assert value["accion"] == action and value["detalles"] == {"resultado": result}
    assert service.KNOWN_ACTIONS == frozenset(ACTIONS)


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("value", ["exito", "rechazado", "synthetic-secret", {}, [], True, 1, None])
def test_07_result_value_and_type_policy(action, value):
    allowed = ({"rechazado"} if action == "login_rechazado" else
               {"exito", "rechazado"} if action == "recuperacion_solicitada" else {"exito"})
    expected = {"resultado": value} if type(value) is str and value in allowed else None
    assert service.safe_details(action, {"resultado": value}) == expected


@pytest.mark.parametrize("details", [None, {}, [], ["synthetic-secret"], "synthetic-secret", 1, True,
    {"resultado": {"token": "synthetic-secret"}}, {"resultado": ["exito", "synthetic-secret"]},
    {"resultado": "exito synthetic-secret"}, {"resultado": None}, {"rol": "synthetic-secret"}])
def test_08_malformed_known_keys_never_escape(client, operator, factory, details):
    id = seed(factory, detalles=details)
    response = client.get(BASE + "/" + id)
    assert response.status_code == 200 and response.json()["detalles"] is None
    assert "synthetic-secret" not in response.text


def test_09_extra_keys_omitted_without_mutating_source(client, operator, factory):
    details = {"resultado": "exito", "rol": "synthetic-secret", "agregadas": ["synthetic-secret"],
               "retiradas": [{"password": "synthetic-secret"}], "token": "synthetic-secret"}
    id = seed(factory, accion="permisos_rol_actualizados", detalles=details)
    response = client.get(BASE + "/" + id)
    assert response.json()["detalles"] == {"resultado": "exito"}
    assert "synthetic-secret" not in response.text
    with factory() as db:
        assert db.get(Bitacora, int(id)).detalles == details


def test_10_unknown_action_hidden(client, operator, factory):
    id = seed(factory, accion="synthetic-secret")
    response = client.get(BASE + "/" + id)
    assert response.json()["accion"] is None and response.json()["detalles"] is None
    assert "synthetic-secret" not in response.text
    response = client.get(BASE)
    assert "synthetic-secret" not in response.text


def test_11_null_and_unrecoverable_actor(client, operator, factory):
    id = seed(factory)
    assert client.get(BASE + "/" + id).json()["usuario_id"] is None
    # Legacy orphan projection: no lookup of Usuario and no assumption of a recoverable actor.
    row = SimpleNamespace(id=1, usuario_id="missing-actor", accion="rol_creado", fecha=INSTANT,
                          ip=None, detalles=None)
    db = Mock()
    db.get_bind.return_value.dialect.name = "postgresql"
    db.execute.return_value.one_or_none.return_value = row
    assert service.detail(db, 1).usuario_id == "missing-actor"
    db.get.assert_not_called()
    assert db.execute.call_count == 1


@pytest.mark.parametrize("value,expected", [(None, None), ("invalid", None), ("127.0.0.1", "127.0.0.1"),
    ("2001:0DB8::1", "2001:db8::1"), ("127.0.0.1/32", None), ("fe80::1%eth0", None),
    (" 127.0.0.1", None), (123, None), ({"ip": "synthetic-secret"}, None),
    (ip_address("127.0.0.1"), "127.0.0.1"), (ip_address("2001:db8::1"), "2001:db8::1"),
    (ip_network("10.0.0.0/24"), None)])
def test_12_ip_validation(value, expected):
    assert service.safe_ip(value) == expected


def test_13_bigint_is_lossless_string(client, operator, factory):
    id = seed(factory, id=9007199254740993)
    assert client.get(BASE + "/" + id).json()["id"] == "9007199254740993"
    assert client.get(BASE, params={"accion": "rol_creado"}).json()["items"][0]["id"] == id


@pytest.mark.parametrize("id,status", [("0", 422), ("-1", 422), ("01", 422), ("1.0", 422),
    ("abc", 422), ("9223372036854775808", 422), ("9223372036854775807", 404)])
def test_14_detail_id_validation(client, operator, id, status):
    response = client.get(BASE + "/" + id)
    assert response.status_code == status and response.headers["Cache-Control"] == "no-store"


def test_15_exact_combined_filters_and_pagination(client, operator, factory):
    ids = [seed(factory, usuario_id=operator) for _ in range(3)]
    seed(factory, accion="rol_actualizado", usuario_id=operator)
    seed(factory)
    filters = {"accion": "rol_creado", "usuario_id": operator, "limit": 2, "offset": 1}
    result = client.get(BASE, params=filters).json()
    assert result["total"] == 3 and [r["id"] for r in result["items"]] == list(reversed(ids))[1:]
    assert result["offset"] == 1 and result["limit"] == 2
    for value in (operator.upper(), " " + operator, "%", "_", "' OR 1=1 --"):
        assert client.get(BASE, params={"usuario_id": value}).json()["total"] == 0


def test_16_order_date_before_id(client, operator, factory):
    first = seed(factory, fecha=INSTANT + timedelta(microseconds=1))
    second = seed(factory)
    result = client.get(BASE, params={"accion": "rol_creado"}).json()
    assert [r["id"] for r in result["items"]] == [first, second]


def test_17_empty_pages_and_limits(client, operator):
    result = client.get(BASE, params={"usuario_id": "missing", "limit": 100}).json()
    assert result == dict(items=[], total=0, offset=0, limit=100)
    result = client.get(BASE, params={"offset": 999}).json()
    assert result["items"] == [] and result["total"] > 0


@pytest.mark.parametrize("params", [{"offset": -1}, {"offset": "1.5"}, {"offset": 9223372036854775808},
    {"limit": 0}, {"limit": 101}, {"limit": "1.5"}, {"usuario_id": ""}, {"usuario_id": "x" * 101},
    {"accion": "unknown"}, {"ip": "127.0.0.1"}, {"q": "synthetic-secret"}, {"detalles": "secret"}])
def test_18_invalid_filters(client, operator, params):
    response = client.get(BASE, params=params)
    assert response.status_code == 422
    assert response.json() == {"error": {"code": "datos_invalidos", "message": "Los datos enviados no son válidos"}}


@pytest.mark.parametrize("date", ["2026-09-12", "2026-09-12T12:30:45", "1234567890", "2026-02-30T00:00:00Z",
    "2026-09-12T12:30:45+24:00", "2026-09-12T12:30:45+00:60", "2026-09-12T12:30:45.1234567Z",
    "0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"])
@pytest.mark.parametrize("field", ["desde", "hasta"])
def test_19_invalid_dates(client, operator, date, field):
    assert client.get(BASE, params={field: date}).status_code == 422


def test_20_utc_offsets_inclusive_microsecond_range(client, operator, factory):
    id = seed(factory)
    equivalent = "2026-09-12T08:30:45.123456-04:00"
    params = dict(accion="rol_creado", desde=equivalent, hasta="2026-09-12T14:30:45.123456+02:00")
    result = client.get(BASE, params=params)
    assert result.status_code == 200 and [r["id"] for r in result.json()["items"]] == [id]
    assert client.get(BASE, params=params | {"desde": "2026-09-12T12:30:45.123457Z"}).status_code == 422
    for bounds in ({"desde": "2026-09-12T12:30:45.123457Z"}, {"hasta": "2026-09-12T12:30:45.123455Z"}):
        assert client.get(BASE, params={"accion": "rol_creado"} | bounds).json()["total"] == 0


def test_21_postgres_aware_and_sqlite_naive_policy():
    row = SimpleNamespace(id=1, usuario_id=None, accion="rol_creado", ip=None, fecha=INSTANT.replace(tzinfo=None))
    with pytest.raises(DomainError) as error:
        service.summary(row, "postgresql")
    assert error.value.status == 500
    assert service.summary(row, "sqlite")["fecha"] == INSTANT
    assert row.fecha.tzinfo is None
    row.fecha = INSTANT.astimezone(timezone(timedelta(hours=-4)))
    assert BitacoraResumen(**service.summary(row, "postgresql")).model_dump(mode="json")["fecha"] == "2026-09-12T12:30:45.123456Z"


def test_22_postgres_parameterized_predicates_without_connection():
    filters = BitacoraFiltros(usuario_id="' OR 1=1 --", accion="rol_creado", desde="2026-09-12T08:30:45.123456-04:00")
    statement = select(Bitacora.id).where(*repository.predicates(filters, "postgresql"))
    compiled = statement.compile(dialect=postgresql.dialect())
    assert "OR 1=1" not in str(compiled) and "JOIN" not in str(compiled)
    assert "' OR 1=1 --" in compiled.params.values()
    assert INSTANT in compiled.params.values()


def test_23_reads_need_no_csrf_and_cors_no_store(client, operator, factory):
    id = seed(factory)
    client.headers.pop("X-CSRF-Protection")
    for path in (BASE, BASE + "/" + id, BASE + "/9999", BASE + "?limit=0"):
        response = client.get(path)
        assert response.status_code in (200, 404, 422)
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Pragma"] == "no-cache"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Access-Control-Allow-Origin"] == "http://localhost:4200"
    response = client.options(BASE)
    assert response.status_code == 204 and response.headers["Access-Control-Allow-Methods"] == "GET, OPTIONS"
    assert response.headers["Cache-Control"] == "no-store"
    assert client.options(BASE, headers={"Origin": "https://untrusted.example"}).status_code == 403
    assert "Access-Control-Allow-Origin" not in client.get(BASE, headers={"Origin": "https://untrusted.example"}).headers
    client.headers.pop("Origin")
    assert client.get(BASE).status_code == 200


def test_24_only_two_get_routes(app, client, operator):
    routes = {path: set(operations) for path, operations in app.openapi()["paths"].items() if path.startswith(BASE)}
    assert routes == {BASE: {"get"}, BASE + "/{id}": {"get"}}
    for method in ("POST", "PATCH", "PUT", "DELETE"):
        assert client.request(method, BASE).status_code == 405
        assert client.request(method, BASE + "/1").status_code == 405


def test_25_pure_reads_no_audit_commit_or_mutation(client, operator, factory, monkeypatch):
    id = seed(factory)
    forbidden = Mock(side_effect=AssertionError("Write forbidden during CU08"))
    for name in ("record", "record_role"):
        monkeypatch.setattr(writer, name, forbidden)
    monkeypatch.setattr(Session, "commit", forbidden)
    original_flush = Session.flush
    def clean_flush(db, *args, **kwargs):
        assert not db.new and not db.dirty and not db.deleted
        return original_flush(db, *args, **kwargs)
    monkeypatch.setattr(Session, "flush", clean_flush)
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    engine = factory.kw["bind"]
    event.listen(engine, "before_cursor_execute", capture)
    try:
        # SQLAlchemy checks autoflush even on reads; a clean check is not a write.
        assert client.get(BASE).status_code == 200
        assert client.get(BASE + "/" + id).status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    forbidden.assert_not_called()
    assert statements and all(sql.lstrip().upper().startswith("SELECT") for sql in statements)
    audit_queries = [sql for sql in statements if "FROM bitacora" in sql]
    assert len(audit_queries) == 3 and all("JOIN" not in sql.upper() for sql in audit_queries)


def test_permission_revocation_is_immediate(client, operator, factory):
    with factory.begin() as db:
        db.delete(db.get(RolFuncion, ("auditor", "CU08")))
    for path in (BASE, BASE + "/1"):
        response = client.get(path)
        assert response.status_code == 403 and response.headers["Cache-Control"] == "no-store"
