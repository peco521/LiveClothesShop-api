import hashlib
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from app.core.config import Settings
from app.main import create_app
from app.modules.seguridad_accesos.models import Sesion, Usuario


def test_postgresql_mapping():
    ddl = str(CreateTable(Sesion.__table__).compile(dialect=postgresql.dialect()))
    assert "GENERATED ALWAYS AS IDENTITY" in ddl
    assert "TIMESTAMP WITH TIME ZONE" in ddl
    assert "ON DELETE RESTRICT" in ddl
    assert "credencial_digest ~" in ddl
    assert Usuario.__table__.c.nrorol.type.length == 15


def test_schema_move_integrity():
    workspace = Path(__file__).resolve().parents[2]
    source = workspace / "database/schema.sql"
    assert not (workspace / ".opencode/agent/database/schema.sql").exists()
    sql = source.read_text(encoding="utf-8")
    assert len(re.findall(r"create table", sql, re.I)) == 40
    unchanged = re.sub(r"create table (usuario|sucursal)\s*\(.*?\);", "", sql, flags=re.I | re.S)
    unchanged = re.sub(r"CREATE TABLE sesion \(.*?(?=CREATE TABLE color)", "", unchanged, flags=re.S)
    assert hashlib.sha256(unchanged.strip().encode()).hexdigest() == "563907bf9e84c138f9e772d3980690704f3b0d02db99d195bab8f95a45990abe"
    assert "contrasena text not null" in sql
    assert "activo boolean not null default true" in sql
    assert "nombres varchar(100)" in sql
    assert sql.count("on update cascade on delete restrict") == 2


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
