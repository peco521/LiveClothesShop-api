import json
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.core.security import digest, utcnow
from app.modules.seguridad_accesos.models import Bitacora, Cliente, Funcion, Rol, RolFuncion, Usuario
from app.modules.seguridad_accesos.repositories import cliente
from app.modules.seguridad_accesos.repositories import usuario as usuario_repository
from app.modules.seguridad_accesos.services import auth


def count(db, model):
    return db.scalar(select(func.count()).select_from(model))


def test_registration(client, factory, registration, app, settings):
    registration["correo"] = "  ANA@EXAMPLE.COM "
    response = client.post("/api/auth/registro", json=registration)
    assert response.status_code == 201
    assert response.json()["correo"] == "ana@example.com"
    # CU01: el registro público deja la sesión iniciada, con el mismo contrato
    # que /api/auth/login (cookie HttpOnly + permisos vigentes del rol público).
    session = response.json()["sesion"]
    assert session["usuario"]["idUsuario"] == response.json()["idUsuario"]
    assert session["rol"]["nro"] == "cliente" and session["permisos"] == []
    cookie = client.cookies.get(settings.cookie_name)
    assert cookie is not None and len(cookie) == 43
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "Path=/api" in response.headers["set-cookie"]
    assert client.get("/api/auth/me").json()["usuario"]["idUsuario"] == response.json()["idUsuario"]
    with factory() as db:
        user = db.get(Usuario, response.json()["idUsuario"])
        assert (user.tipo, user.nrorol) == ("C", "cliente")
        assert user.nombre == registration["nombres"]
        assert user.contrasena.startswith("$argon2id$")
        assert app.state.passwords.verify(user.contrasena, registration["contrasena"])
        profile = db.get(Cliente, user.idusuario)
        assert len(profile.cod_cl) == 10
        assert profile.estado == "frecuente"
        assert len(client.app.state.access_tokens._entries) == 1
        assert count(db, Bitacora) == 1


def test_duplicate_email(client, factory, registered, registration):
    registration["correo"] = " ANA@EXAMPLE.COM "
    response = client.post("/api/auth/registro", json=registration)
    assert response.status_code == 409
    with factory() as db:
        assert count(db, Usuario) == count(db, Cliente) == 1


def test_database_duplicate_after_precheck(client, factory, registered, registration, monkeypatch):
    original = usuario_repository.by_email
    calls = 0

    def race(db, email):
        nonlocal calls
        calls += 1
        return [] if calls == 1 else original(db, email)

    monkeypatch.setattr(usuario_repository, "by_email", race)
    assert client.post("/api/auth/registro", json=registration).status_code == 409
    with factory() as db:
        assert count(db, Usuario) == count(db, Cliente) == count(db, Bitacora) == 1


@pytest.mark.parametrize("password", ["corta", " " * 12, "x" * 129])
def test_invalid_password(client, factory, registration, password):
    registration["contrasena"] = password
    response = client.post("/api/auth/registro", json=registration)
    assert response.status_code == 422
    assert "input" not in response.text and "contrasena" not in response.text
    with factory() as db:
        assert count(db, Usuario) == 0


@pytest.mark.parametrize("field,value", [
    ("rol", "Administrador"), ("nroRol", "Administrador"), ("nrorol", "Administrador"),
    ("tipo", "A"), ("tipo", "E"), ("tipo", "Administrador"),
    ("tipo", "Empleado"), ("permisos", ["administrar"]), ("activo", False),
])
def test_forbidden_public_fields(client, factory, registration, field, value):
    registration[field] = value
    assert client.post("/api/auth/registro", json=registration).status_code == 422
    with factory() as db:
        assert count(db, Usuario) == count(db, Cliente) == 0


@pytest.mark.parametrize("failure", ["runtime", "integrity"])
def test_profile_failure_rolls_back(client, factory, registration, monkeypatch, failure):
    def fail(db, user_id, code):
        assert db.get(Usuario, user_id) is not None
        if failure == "integrity":
            raise IntegrityError("insert", None, Exception("simulated"))
        raise RuntimeError("simulated")
    monkeypatch.setattr(cliente, "add", fail)
    assert client.post("/api/auth/registro", json=registration).status_code in {500, 503}
    with factory() as db:
        assert count(db, Usuario) == count(db, Cliente) == count(db, Bitacora) == 0


def test_actual_profile_constraint_rolls_back(client, factory, registration, monkeypatch):
    def invalid_profile(db, user_id, code):
        db.add(Cliente(idusuario=user_id, cod_cl=None))
        db.flush()
    monkeypatch.setattr(cliente, "add", invalid_profile)
    assert client.post("/api/auth/registro", json=registration).status_code == 503
    with factory() as db:
        assert count(db, Usuario) == count(db, Cliente) == count(db, Bitacora) == 0


def test_unsafe_public_role_rejected(client, factory, registration):
    with factory.begin() as db:
        db.add(Funcion(id="test_perm", descripcion="Prueba"))
        db.flush()
        db.add(RolFuncion(nrorol="cliente", idfun="test_perm", descripcion="Prueba"))
    assert client.post("/api/auth/registro", json=registration).status_code == 503
    with factory() as db:
        assert count(db, Usuario) == 0


def test_missing_public_role_rejected(client, factory, registration):
    with factory.begin() as db:
        db.delete(db.get(Rol, "cliente"))
    assert client.post("/api/auth/registro", json=registration).status_code == 503


def test_public_role_used_by_internal_user_rejected(client, factory, registered, registration):
    with factory.begin() as db:
        db.get(Usuario, registered["idUsuario"]).tipo = "E"
    registration["correo"] = "new@example.com"
    assert client.post("/api/auth/registro", json=registration).status_code == 503


def test_login_and_digest(client, factory, registered, credentials, settings):
    response = client.post("/api/auth/login", json=credentials)
    assert response.status_code == 200
    assert response.json()["usuario"]["idUsuario"] == registered["idUsuario"]
    assert response.json()["rol"]["nro"] == "cliente"
    assert response.json()["permisos"] == []
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=lax" in response.headers["set-cookie"]
    assert "Path=/api" in response.headers["set-cookie"]
    credential = client.cookies.get(settings.cookie_name)
    assert len(credential) == 43
    with factory() as db:
        store = client.app.state.access_tokens
        access = store.get(credential)
        assert digest(credential) in store._entries and credential not in store._entries
        assert access.usuario_id == registered["idUsuario"]
        assert timedelta(hours=7, minutes=59) < access.expira_en - utcnow() <= timedelta(hours=8)
        assert len(store._entries) == 1
    assert client.get("/api/auth/me").status_code == 200


@pytest.mark.parametrize("case", ["wrong_password", "absent", "legacy_hash"])
def test_generic_login_failure(client, factory, registered, credentials, case):
    if case == "wrong_password":
        credentials["contrasena"] = "Otra frase incorrecta"
    elif case == "absent":
        credentials["correo"] = "absent@example.com"
    else:
        with factory.begin() as db:
            user = db.get(Usuario, registered["idUsuario"])
            user.contrasena = "unsupported-legacy-value"
    response = client.post("/api/auth/login", json=credentials)
    assert response.status_code == 401
    assert response.json() == {"error": {"code": "autenticacion_rechazada",
                                        "message": "No se pudo autenticar la solicitud"}}
    assert "set-cookie" not in response.headers
    with factory() as db:
        assert not client.app.state.access_tokens._entries
        event = db.scalar(select(Bitacora).where(Bitacora.accion == "login_rechazado"))
        assert event.usuario_id is None
        assert event.detalles == {"resultado": "rechazado"}


@pytest.mark.parametrize("case", ["expired", "revoked", "password_changed", "tampered"])
def test_reject_invalid_session(client, factory, registered, credentials, settings, case, monkeypatch):
    assert client.post("/api/auth/login", json=credentials).status_code == 200
    token = client.cookies.get(settings.cookie_name)
    if case == "tampered":
        client.cookies.clear()
        client.cookies.set(settings.cookie_name, "x" * 43, path="/api")
    elif case == "expired":
        monkeypatch.setattr("app.core.access_tokens.utcnow", lambda: utcnow() + timedelta(hours=9))
    elif case == "revoked":
        client.app.state.access_tokens.revoke(token)
    else:
        with factory.begin() as db:
            db.get(Usuario, registered["idUsuario"]).contrasena = "changed-password-hash"
    assert client.get("/api/auth/me").status_code == 401


def test_no_credentials(client):
    assert client.get("/api/auth/me").status_code == 401


def test_bearer_and_ambiguous_identity(client, registered, credentials, settings):
    client.post("/api/auth/login", json=credentials)
    token = client.cookies.get(settings.cookie_name)
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/auth/me", headers=headers).status_code == 401
    client.cookies.clear()
    assert client.get("/api/auth/me", headers=headers).status_code == 200


def test_current_permissions_read_from_database(client, factory, registered, credentials):
    client.post("/api/auth/login", json=credentials)
    with factory.begin() as db:
        db.add(Rol(nro="test_role", descripcion="Prueba"))
        db.add(Funcion(id="test_perm", descripcion="Prueba"))
        db.flush()
        db.add(RolFuncion(nrorol="test_role", idfun="test_perm", descripcion="Prueba"))
        db.get(Usuario, registered["idUsuario"]).nrorol = "test_role"
    result = client.get("/api/auth/me").json()
    assert result["rol"]["nro"] == "test_role"
    assert result["permisos"] == ["test_perm"]


def test_no_sensitive_response_audit_or_logs(client, factory, registered, credentials, settings, caplog):
    response = client.post("/api/auth/login", json=credentials)
    token = client.cookies.get(settings.cookie_name)
    with factory() as db:
        encoded = db.get(Usuario, registered["idUsuario"]).contrasena
        stored_digest = digest(token)
        events = list(db.scalars(select(Bitacora)))
        audit = json.dumps([{ "accion": e.accion, "detalles": e.detalles} for e in events])
    output = response.text + client.get("/api/auth/me").text + audit + caplog.text
    for secret in [credentials["contrasena"], encoded, token, stored_digest]:
        assert secret not in output
    for key in ["contrasena", "digest", "token", "hash"]:
        assert key not in response.text.lower()
    assert response.headers["cache-control"] == "no-store"


def test_audit_failure_rolls_back_login(client, factory, registered, credentials, monkeypatch, caplog):
    def fail(*args):
        raise RuntimeError(credentials["contrasena"])
    monkeypatch.setattr(auth, "record", fail)
    result = client.post("/api/auth/login", json=credentials)
    assert result.status_code == 500
    assert "set-cookie" not in result.headers
    assert credentials["contrasena"] not in result.text + caplog.text
    with factory() as db:
        assert not client.app.state.access_tokens._entries


def test_login_creates_independent_random_sessions(client, factory, registered, credentials, settings):
    client.post("/api/auth/login", json=credentials)
    first = client.cookies.get(settings.cookie_name)
    client.post("/api/auth/login", json=credentials)
    second = client.cookies.get(settings.cookie_name)
    assert first != second
    with factory() as db:
        assert client.app.state.access_tokens.get(first) is not None
        assert client.app.state.access_tokens.get(second) is not None


def test_validation_does_not_echo_malicious_input(client, registration):
    registration["secreto"] = "sensitive-marker-never-echo"
    response = client.post("/api/auth/registro", json=registration)
    assert response.status_code == 422
    assert registration["secreto"] not in response.text
    assert registration["contrasena"] not in response.text


@pytest.mark.parametrize("path", ["/api/auth/registro", "/api/auth/login", "/api/auth/logout"])
def test_csrf(client, registration, path):
    response = client.post(path, json=registration, headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    client.headers.pop("X-CSRF-Protection")
    assert client.post(path, json=registration).status_code == 403


def test_cors_preflight(client):
    response = client.options("/api/auth/login", headers={
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type,x-csrf-protection"})
    assert response.status_code == 204
    assert response.headers["access-control-allow-origin"] == "http://localhost:4200"
    assert response.headers["access-control-allow-credentials"] == "true"
    assert client.options("/api/auth/login", headers={"Origin": "https://evil.example"}).status_code == 403


def test_existing_endpoints(client):
    assert client.get("/").status_code == 200
    assert client.get("/health").json() == {"status": "ok"}


def test_no_future_use_case_routes(app):
    paths = app.openapi()["paths"]
    assert "post" in paths["/api/auth/logout"]
    assert not any(path in paths for path in ["/api/auth/recuperacion", "/api/usuarios", "/api/bitacora"])
