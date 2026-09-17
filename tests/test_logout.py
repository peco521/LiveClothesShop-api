import json
from datetime import timedelta
from http.cookies import SimpleCookie

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.security import digest, utcnow
from app.main import create_app
from app.modules.seguridad_accesos.models import Bitacora, Funcion, RolFuncion, Usuario
from app.modules.seguridad_accesos.services import auth


@pytest.mark.parametrize("transport", ["cookie", "bearer"])
def test_logout_current_session_only(client, factory, registered, credentials, settings, transport, caplog):
    assert client.post("/api/auth/login", json=credentials).status_code == 200
    other = client.cookies.get(settings.cookie_name)
    assert client.post("/api/auth/login", json=credentials).status_code == 200
    current = client.cookies.get(settings.cookie_name)
    headers = {}
    if transport == "bearer":
        client.cookies.clear()
        headers = {"Authorization": f"Bearer {current}"}
    me = client.get("/api/auth/me", headers=headers)
    assert me.status_code == 200 and me.json()["permisos"] == []
    result = client.post("/api/auth/logout", headers=headers)
    assert result.status_code == 204 and result.content == b""
    assert result.headers["cache-control"] == "no-store"
    assert client.cookies.get(settings.cookie_name) is None
    assert client.get("/api/auth/me", headers=headers).status_code == 401
    # Neither transport can resurrect the revoked access token.
    client.cookies.set(settings.cookie_name, current, path="/api")
    assert client.get("/api/auth/me").status_code == 401
    assert client.post("/api/auth/logout").status_code == 401
    client.cookies.clear()
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {current}"}).status_code == 401
    client.cookies.set(settings.cookie_name, other, path="/api")
    assert client.get("/api/auth/me").status_code == 200
    with factory() as db:
        assert client.app.state.access_tokens.get(current) is None
        assert client.app.state.access_tokens.get(other) is not None
        events = list(db.scalars(select(Bitacora).where(Bitacora.accion == "logout_correcto")))
        assert len(events) == 1
        assert events[0].usuario_id == registered["idUsuario"]
        assert events[0].detalles == {"resultado": "exito"}
        assert list(db.scalars(select(Funcion))) == []
        assert list(db.scalars(select(RolFuncion))) == []
        audit = json.dumps([{"accion": e.accion, "detalles": e.detalles} for e in events])
        output = result.text + str(dict(result.headers)) + audit + caplog.text
        for secret in [current, other, digest(current), digest(other), credentials["contrasena"],
                       db.get(Usuario, registered["idUsuario"]).contrasena]:
            assert secret not in output


@pytest.mark.parametrize("case", ["missing", "malformed", "unknown", "expired", "revoked", "password_changed", "ambiguous", "scheme"])
def test_logout_requires_valid_session(client, factory, registered, credentials, settings, case, monkeypatch):
    client.post("/api/auth/login", json=credentials)
    current = client.cookies.get(settings.cookie_name)
    headers = {}
    if case in {"missing", "malformed", "unknown"}:
        client.cookies.clear()
        if case != "missing":
            client.cookies.set(settings.cookie_name, "invalid" if case == "malformed" else "x" * 43, path="/api")
    elif case in {"ambiguous", "scheme"}:
        headers = {"Authorization": f"{'Bearer' if case == 'ambiguous' else 'Basic'} {current}"}
        if case == "scheme":
            client.cookies.clear()
    elif case == "expired":
        monkeypatch.setattr("app.core.access_tokens.utcnow", lambda: utcnow() + timedelta(hours=9))
    elif case == "revoked":
        client.app.state.access_tokens.revoke(current)
    else:
        with factory.begin() as db:
            db.get(Usuario, registered["idUsuario"]).contrasena = "changed-password-hash"
    result = client.post("/api/auth/logout", headers=headers)
    assert result.status_code == 401
    assert result.json()["error"]["code"] == "autenticacion_rechazada"
    assert "set-cookie" not in result.headers
    with factory() as db:
        assert db.scalar(select(Bitacora).where(Bitacora.accion == "logout_correcto")) is None


@pytest.mark.parametrize("transport", ["cookie", "bearer"])
@pytest.mark.parametrize("case", ["origin_missing", "origin_bad", "csrf_missing", "csrf_bad"])
def test_logout_csrf_does_not_revoke(client, factory, registered, credentials, settings, transport, case):
    client.post("/api/auth/login", json=credentials)
    if transport == "bearer":
        client.headers["Authorization"] = f"Bearer {client.cookies.get(settings.cookie_name)}"
        client.cookies.clear()
    header = "Origin" if case.startswith("origin") else "X-CSRF-Protection"
    if case.endswith("missing"):
        client.headers.pop(header)
    else:
        client.headers[header] = "https://evil.example" if header == "Origin" else "0"
    result = client.post("/api/auth/logout")
    if transport == "bearer":
        # Política móvil: Bearer nativo sin cookie no usa defensa CSRF de
        # cookie (el token no es ambient); Origin ausente/maligno se ignora y
        # la sesión se revoca normalmente.
        assert result.status_code == 204
        assert client.get("/api/auth/me").status_code == 401
        return
    assert result.status_code == 403
    assert "set-cookie" not in result.headers
    assert client.get("/api/auth/me").status_code == 200
    with factory() as db:
        assert db.scalar(select(Bitacora).where(Bitacora.accion == "logout_correcto")) is None


@pytest.mark.parametrize("secure,samesite", [(False, "lax"), (True, "strict"), (True, "none")])
def test_cookie_deletion_matches_login(settings, factory, registration, credentials, secure, samesite):
    settings = settings.model_copy(update={"cookie_secure": secure, "cookie_samesite": samesite,
                                           "cookie_name": "test_session"})
    with TestClient(create_app(settings, factory), base_url="https://testserver") as client:
        client.headers.update({"Origin": "http://localhost:4200", "X-CSRF-Protection": "1"})
        assert client.post("/api/auth/registro", json=registration).status_code == 201
        login = client.post("/api/auth/login", json=credentials)
        created = SimpleCookie(login.headers["set-cookie"])[settings.cookie_name]
        result = client.post("/api/auth/logout")
        assert result.status_code == 204
        deleted = SimpleCookie(result.headers["set-cookie"])[settings.cookie_name]
        for attribute in ["path", "domain", "samesite", "secure", "httponly"]:
            assert deleted[attribute] == created[attribute]
        assert deleted["path"] == "/api" and deleted["domain"] == ""
        assert deleted.value == "" and deleted["max-age"] == "0"
        assert deleted["expires"]
        assert client.cookies.get(settings.cookie_name) is None
        assert client.get("/api/auth/me").status_code == 401


def test_logout_audit_failure_rolls_back(client, factory, registered, credentials, settings, monkeypatch, caplog):
    client.post("/api/auth/login", json=credentials)
    current = client.cookies.get(settings.cookie_name)
    def fail(*args):
        raise RuntimeError(current)
    monkeypatch.setattr(auth, "record", fail)
    result = client.post("/api/auth/logout")
    assert result.status_code == 500
    assert "set-cookie" not in result.headers
    assert current not in result.text + caplog.text
    assert client.get("/api/auth/me").status_code == 200
    with factory() as db:
        assert db.scalar(select(Bitacora).where(Bitacora.accion == "logout_correcto")) is None
