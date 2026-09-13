import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.core.database import Base
from app.main import create_app
from app.modules.seguridad_accesos.models import Rol


@pytest.fixture
def settings():
    # This URL is deliberately never used: all tests inject the SQLite factory.
    return Settings(_env_file=None, database_url="postgresql+psycopg://unused@localhost/unused",
                    cliente_rol_id="cliente", environment="test",
                    allowed_origins=["http://localhost:4200"])


@pytest.fixture
def factory():
    engine = create_engine("sqlite+pysqlite:///:memory:",
                           connect_args={"check_same_thread": False}, poolclass=StaticPool,
                           hide_parameters=True)

    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as db:
        db.add(Rol(nro="cliente", descripcion="Cliente"))
    yield factory
    engine.dispose()


@pytest.fixture
def app(settings, factory):
    return create_app(settings, factory)


@pytest.fixture
def client(app):
    with TestClient(app, raise_server_exceptions=False) as client:
        client.headers.update({"Origin": "http://localhost:4200", "X-CSRF-Protection": "1"})
        yield client


@pytest.fixture
def registration():
    return {"ci": "1234567", "nombres": "Ana María", "apellidoPat": "Pérez",
            "apellidoMat": "Gómez", "sexo": "F", "correo": "ana@example.com",
            "telefono": "70000000", "direccion": "Calle de prueba 10",
            "fechaNac": "2000-01-01", "contrasena": "Frase de prueba larga 123!"}


@pytest.fixture
def registered(client, registration):
    result = client.post("/api/auth/registro", json=registration)
    assert result.status_code == 201
    return result.json()


@pytest.fixture
def credentials(registration):
    return {"correo": registration["correo"], "contrasena": registration["contrasena"]}
