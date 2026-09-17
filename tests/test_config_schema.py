import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from app.core.config import Settings
from app.main import create_app
from app.core.database import Base
from app.modules.seguridad_accesos.models import Usuario


def test_postgresql_mapping():
    ddl = str(CreateTable(Usuario.__table__).compile(dialect=postgresql.dialect()))
    assert "nombre VARCHAR(100)" in ddl
    assert "nombres" not in ddl and "activo" not in ddl
    assert Usuario.__table__.c.nrorol.type.length == 15


def test_schema_does_not_require_session_table(factory):
    from sqlalchemy import inspect
    assert "sesion" not in Base.metadata.tables
    tables = inspect(factory.kw["bind"]).get_table_names()
    assert "sesion" not in tables
    columns = {column["name"] for column in inspect(factory.kw["bind"]).get_columns("usuario")}
    assert "nombre" in columns
    assert columns.isdisjoint({"activo", "nombres"})


@pytest.mark.parametrize("overrides", [
    {"environment": "production", "cookie_secure": False},
    {"cookie_samesite": "none", "cookie_secure": False},
    {"allowed_origins": ["*"]},
    {"allowed_origins": ["https://site.example/path"]},
    {"session_hours": 0},
])
def test_invalid_configuration(settings, overrides):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **(settings.model_dump() | overrides))


def test_secure_cookie_and_configurable_expiry(settings, factory, registration):
    settings = Settings(_env_file=None, **(settings.model_dump() | {
        "environment": "production", "cookie_secure": True,
        "allowed_origins": ["https://shop.example"], "session_hours": 2}))
    app = create_app(settings, factory)
    with TestClient(app, base_url="https://shop.example") as client:
        client.headers.update({"Origin": "https://shop.example", "X-CSRF-Protection": "1"})
        assert client.post("/api/auth/registro", json=registration).status_code == 201
        response = client.post("/api/auth/login", json={
            "correo": registration["correo"], "contrasena": registration["contrasena"]})
        assert response.status_code == 200
        cookie = response.headers["set-cookie"]
        assert "Secure" in cookie and "HttpOnly" in cookie and "Max-Age=7200" in cookie
        assert client.get("/api/auth/me").status_code == 200
