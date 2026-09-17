from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.security import digest, utcnow
from app.modules.seguridad_accesos.models import Bitacora, RecuperacionContrasena, Usuario
from app.modules.seguridad_accesos.services import auth


REQUEST = "/api/auth/recuperar-contrasena"
RESET = "/api/auth/restablecer-contrasena"
NEW_PASSWORD = "Nueva frase de prueba 456!"


class FakeDelivery:
    # Test-only, memory-only capture; never installed by application startup.
    def __init__(self):
        self.calls = []

    def dispatch(self, recipient, token, expires):
        self.calls.append((recipient, token, expires))


@pytest.fixture
def delivery(client, app):
    fake = FakeDelivery()
    app.state.recovery_delivery = fake
    return fake


@pytest.fixture
def token(client, registered, delivery, registration):
    assert client.post(REQUEST, json={"correo": registration["correo"]}).status_code == 202
    return delivery.calls[-1][1]


def reset(client, token, password=NEW_PASSWORD):
    return client.post(RESET, json={"token": token, "nueva_contrasena": password})


def test_indistinguishable(client, registered, delivery, registration, factory):
    existing = client.post(REQUEST, json={"correo": registration["correo"]})
    absent = client.post(REQUEST, json={"correo": "absent@example.com"})
    assert existing.status_code == absent.status_code == 202
    assert existing.json() == absent.json()
    assert len(delivery.calls) == 2
    assert delivery.calls[0][0] == registration["correo"]
    assert delivery.calls[1][0] is None
    assert "set-cookie" not in existing.headers


def test_only_digest_and_ttl(token, factory):
    with factory() as db:
        row = db.scalar(select(RecuperacionContrasena))
        assert row.token_digest == digest(token)
        assert row.token_digest != token
        assert row.expira_en - row.creada_en == timedelta(minutes=30)
        assert row.utilizada_en is None


def test_valid_argon2_no_autologin(client, token, factory, app, registered):
    result = reset(client, token)
    assert result.status_code == 200
    assert "set-cookie" not in result.headers
    with factory() as db:
        user = db.get(Usuario, registered["idUsuario"])
        assert user.contrasena.startswith("$argon2id$")
        assert app.state.passwords.verify(user.contrasena, NEW_PASSWORD)
        assert db.scalar(select(RecuperacionContrasena)).utilizada_en is not None
    assert client.get("/api/auth/me").status_code == 401


@pytest.mark.parametrize("state", ["expired", "consumed", "invalid"])
def test_rejected_token(client, token, factory, state):
    with factory.begin() as db:
        row = db.scalar(select(RecuperacionContrasena))
        if state == "expired":
            row.creada_en = utcnow() - timedelta(hours=2)
            row.expira_en = utcnow() - timedelta(hours=1)
        elif state == "consumed":
            row.utilizada_en = utcnow()
    assert reset(client, "x" * 43 if state == "invalid" else token).status_code == 400


@pytest.mark.parametrize("password", ["short", " " * 12, "x" * 129])
def test_weak_password_does_not_consume(client, token, factory, password):
    result = reset(client, token, password)
    assert result.status_code == 422
    assert token not in result.text and password not in result.text
    with factory() as db:
        assert db.scalar(select(RecuperacionContrasena)).utilizada_en is None


def test_all_sessions_revoked(client, token, credentials, factory, settings):
    accesses = []
    for _ in range(3):
        assert client.post("/api/auth/login", json=credentials).status_code == 200
        accesses.append(client.cookies.get(settings.cookie_name))
    assert len(set(accesses)) == 3
    assert reset(client, token).status_code == 200
    assert client.get("/api/auth/me").status_code == 401
    client.cookies.clear()
    for access in accesses:
        assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {access}"}).status_code == 401
    assert client.post("/api/auth/login", json=credentials).status_code == 401
    assert client.post("/api/auth/login", json=credentials | {"contrasena": NEW_PASSWORD}).status_code == 200


def test_missing_account_cannot_reset(client, token, factory, registered, monkeypatch):
    with factory() as db:
        previous = db.get(Usuario, registered["idUsuario"]).contrasena
    monkeypatch.setattr(auth.usuario, "locked", lambda *args: None)
    assert reset(client, token).status_code == 400
    with factory() as db:
        assert db.get(Usuario, registered["idUsuario"]).contrasena == previous
        assert db.scalar(select(RecuperacionContrasena)).utilizada_en is None


def test_rollback(client, token, factory, registered, credentials, monkeypatch):
    client.post("/api/auth/login", json=credentials)
    with factory() as db:
        previous = db.get(Usuario, registered["idUsuario"]).contrasena
    def fail(*args):
        raise RuntimeError("synthetic private diagnostics")
    monkeypatch.setattr(auth, "record", fail)
    result = reset(client, token)
    assert result.status_code == 500
    assert "synthetic" not in result.text
    with factory() as db:
        assert db.get(Usuario, registered["idUsuario"]).contrasena == previous
        assert db.scalar(select(RecuperacionContrasena)).utilizada_en is None
    assert client.get("/api/auth/me").status_code == 200


def test_audit_no_credentials(client, token, factory, registration, caplog):
    assert reset(client, token).status_code == 200
    with factory() as db:
        events = list(db.scalars(select(Bitacora)))
        request = next(event for event in events if event.accion == "recuperacion_solicitada")
        assert request.usuario_id is None
        success = next(event for event in events if event.accion == "contrasena_restablecida")
        assert success.usuario_id is not None
        details = str([event.detalles for event in events])
        for private in [token, NEW_PASSWORD, registration["correo"], registration["contrasena"]]:
            assert private not in details and private not in caplog.text


def test_validation_redacted(client, token):
    result = client.post(RESET, json={"token": token, "nueva_contrasena": NEW_PASSWORD, "extra": token})
    assert result.status_code == 422
    assert token not in result.text and NEW_PASSWORD not in result.text


def test_public_no_permissions(client, token):
    client.cookies.clear()
    assert reset(client, token).status_code == 200


def test_reissue_invalidates_old(client, token, delivery, registration):
    assert client.post(REQUEST, json={"correo": registration["correo"]}).status_code == 202
    new_token = delivery.calls[-1][1]
    assert token != new_token
    assert reset(client, token).status_code == 400
    assert reset(client, new_token).status_code == 200
    assert reset(client, new_token).status_code == 400


def test_disabled_uniform(client, registered, registration):
    first = client.post(REQUEST, json={"correo": registration["correo"]})
    second = client.post(REQUEST, json={"correo": "absent@example.com"})
    assert first.status_code == second.status_code == 503
    assert first.json() == second.json()


def test_delivery_failure_rolls_back(client, token, delivery, registration, factory, monkeypatch):
    def fail(*args):
        raise RuntimeError("private delivery failure")
    monkeypatch.setattr(delivery, "dispatch", fail)
    result = client.post(REQUEST, json={"correo": registration["correo"]})
    assert result.status_code == 503
    assert "private" not in result.text
    with factory() as db:
        rows = list(db.scalars(select(RecuperacionContrasena)))
        assert len(rows) == 1 and rows[0].utilizada_en is None
    assert reset(client, token).status_code == 200


@pytest.mark.parametrize("path,body", [(REQUEST, {"correo": "ana@example.com"}), (RESET, {"token": "x" * 43, "nueva_contrasena": NEW_PASSWORD})])
def test_origin_csrf(client, path, body):
    assert client.post(path, json=body, headers={"Origin": "https://untrusted.example"}).status_code == 403
    assert client.post(path, json=body, headers={"X-CSRF-Protection": "0"}).status_code == 403


def test_request_does_not_change_password_or_create_session(client, registered, delivery, registration, factory):
    with factory() as db:
        previous = db.get(Usuario, registered["idUsuario"]).contrasena
    assert client.post(REQUEST, json={"correo": registration["correo"]}).status_code == 202
    with factory() as db:
        assert db.get(Usuario, registered["idUsuario"]).contrasena == previous
        assert not client.app.state.access_tokens._entries


def test_unknown_accounts_never_issue(client, registered, delivery, registration, factory):
    for email in ["unknown@example.com", "absent@example.com"]:
        assert client.post(REQUEST, json={"correo": email}).status_code == 202
    assert all(recipient is None for recipient, _, _ in delivery.calls)
    with factory() as db:
        assert db.scalar(select(RecuperacionContrasena)) is None


def test_timing_floor_all_addresses(client, registered, delivery, registration, factory, monkeypatch):
    waits = []
    monkeypatch.setattr(auth, "monotonic", lambda: 10)
    monkeypatch.setattr(auth, "sleep", waits.append)
    client.post(REQUEST, json={"correo": registration["correo"]})
    client.post(REQUEST, json={"correo": "absent@example.com"})
    client.post(REQUEST, json={"correo": registration["correo"]})
    assert waits == [0.25, 0.25, 0.25]


def test_postgresql_lock_and_conditional_consume():
    from unittest.mock import Mock
    from sqlalchemy.dialects import postgresql
    from app.modules.seguridad_accesos.repositories import recuperacion_contrasena as recovery, usuario

    db = Mock()
    usuario.locked(db, "synthetic-user")
    query = str(db.scalar.call_args.args[0].compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in query
    db.scalars.return_value = []
    usuario.by_email(db, "ana@example.com", lock=True)
    assert "FOR UPDATE" in str(db.scalars.call_args.args[0].compile(dialect=postgresql.dialect()))
    recovery.consume(db, 1, utcnow())
    query = str(db.execute.call_args.args[0].compile(dialect=postgresql.dialect()))
    assert "utilizada_en IS NULL" in query and "expira_en >" in query


def test_concurrent_single_use_isolated_sqlite(tmp_path, settings, registration, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker
    from app.core.database import Base
    from app.core.errors import DomainError
    from app.modules.seguridad_accesos.models import Rol
    from app.modules.seguridad_accesos.repositories import recuperacion_contrasena as recovery
    from app.modules.seguridad_accesos.schemas.auth import Registro, RecuperarContrasena, RestablecerContrasena
    from app.core.security import Passwords

    # File-backed SQLite gives each worker its own connection; never PostgreSQL.
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'recovery.db'}", hide_parameters=True,
                           connect_args={"timeout": 10})
    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    passwords = Passwords()
    try:
        with factory.begin() as db:
            db.add(Rol(nro="cliente", descripcion="Cliente"))
        with factory() as db:
            auth.register(db, Registro(**registration), settings, passwords, None)
        fake = FakeDelivery()
        with factory() as db:
            auth.request_recovery(db, RecuperarContrasena(correo=registration["correo"]), fake, None)
        data = RestablecerContrasena(token=fake.calls[0][1], nueva_contrasena=NEW_PASSWORD)
        barrier = Barrier(2)
        consume = recovery.consume
        def synchronized_consume(db, token_id, now):
            # Both workers have read the same unused token before either updates it.
            barrier.wait(timeout=10)
            return consume(db, token_id, now)
        monkeypatch.setattr(recovery, "consume", synchronized_consume)
        def run(_):
            with factory() as db:
                try:
                    auth.reset_password(db, data, passwords, None)
                    return 200
                except DomainError as error:
                    return error.status
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(run, range(2))) == [200, 400]
        with factory() as db:
            events = list(db.scalars(select(Bitacora).where(Bitacora.accion == "contrasena_restablecida")))
            assert len(events) == 1
    finally:
        engine.dispose()


def test_disabled_audit_anonymous(client, factory):
    assert client.post(REQUEST, json={"correo": "absent@example.com"}).status_code == 503
    with factory() as db:
        event = db.scalar(select(Bitacora))
        assert event.accion == "recuperacion_solicitada" and event.usuario_id is None
        assert event.detalles == {"resultado": "rechazado"}


def test_delivery_composition_injection(settings, factory, registration):
    from fastapi.testclient import TestClient
    from app.main import create_app

    fake = FakeDelivery()
    app = create_app(settings, factory, recovery_delivery=fake)
    with TestClient(app) as client:
        client.headers.update({"Origin": "http://localhost:4200", "X-CSRF-Protection": "1"})
        assert client.post("/api/auth/registro", json=registration).status_code == 201
        assert client.post(REQUEST, json={"correo": registration["correo"]}).status_code == 202
        assert len(fake.calls) == 1
        assert reset(client, fake.calls[0][1]).status_code == 200
