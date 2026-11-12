"""Pruebas de integración contra PostgreSQL real (cluster de pruebas).

Estas pruebas NO usan SQLite y NO tocan Supabase ni la base actual. Requieren:

  1. `database/saas/00_preparar_postgres_pruebas.ps1` (cluster en 55432).
  2. `LiveClothesShop-api/scripts/seed_demo.py` (empresas de demostración).

Si falta `.local/backend.env` o la base no responde, el módulo se omite: no se
declara "verificado" nada que no se haya ejecutado. SQLite no demuestra RLS.

Ejecutar solo estas pruebas:

    .venv/Scripts/python.exe -m pytest tests/test_saas_postgres.py -v
"""

from __future__ import annotations

import json
import threading
from contextlib import ExitStack
from datetime import date
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.core.errors import DomainError
from app.main import create_app
from app.modules.saas_suscripciones.planes_funcionalidades.models.planes import (
    Plan, PlanFuncionalidad, PlanVersion, SuscripcionPlan, SuscripcionVersion,
)
from app.modules.saas_suscripciones.planes_funcionalidades.schemas.planes import PlanActualizacion
from app.modules.saas_suscripciones.planes_funcionalidades.services import planes as planes_service
from app.modules.seguridad_accesos.cu07_clientes.services import cliente as cu07_cliente_service

RAIZ = Path(__file__).resolve().parents[1]
ENV_PRUEBAS = RAIZ.parent / ".local" / "backend.env"
CREDENCIALES = RAIZ / ".local" / "demo_credentials.json"

EMPRESAS = ("manaco", "kosy-pantalones", "urbanwomen", "kosy-prendas", "lupo")
PERMISOS_SAAS = {"SAAS-EMPRESAS", "SAAS-PLANES", "SAAS-AUDITORIA", "SAAS-DEMO", "SAAS-SOPORTE"}
CABECERAS = {"Origin": "http://localhost:4200", "X-CSRF-Protection": "1"}


def _leer_entorno():
    if not ENV_PRUEBAS.exists():
        return None, None
    valores: dict[str, str] = {}
    for linea in ENV_PRUEBAS.read_text(encoding="utf-8").splitlines():
        if linea.strip() and not linea.strip().startswith("#") and "=" in linea:
            clave, _, valor = linea.partition("=")
            valores[clave.strip()] = valor.strip()
    return valores.get("TEST_DATABASE_URL"), valores.get("TEST_PLATFORM_DATABASE_URL")


APP_URL, PLATFORM_URL = _leer_entorno()


def _validar_url_test(url: str | None, variable: str, usuario: str) -> None:
    if url is None:
        return
    parsed = make_url(url)
    if (parsed.host != "127.0.0.1" or parsed.port != 55432
            or parsed.database != "fashionstore_test" or parsed.username != usuario):
        raise RuntimeError(
            f"{variable} debe usar el rol {usuario} en 127.0.0.1:55432/fashionstore_test")


_validar_url_test(APP_URL, "TEST_DATABASE_URL", "fashionstore_app")
_validar_url_test(PLATFORM_URL, "TEST_PLATFORM_DATABASE_URL", "fashionstore_provisioner")


def _base_disponible() -> bool:
    if not (APP_URL and PLATFORM_URL and CREDENCIALES.exists()):
        return False
    for url, role, bypass in (
        (APP_URL, "fashionstore_app", False),
        (PLATFORM_URL, "fashionstore_provisioner", True),
    ):
        motor = create_engine(url, hide_parameters=True)
        try:
            with motor.connect() as conexion:
                database, current_role, port, bypass_rls = conexion.execute(text("""
                    SELECT current_database(), current_user, inet_server_port(),
                           (SELECT rolbypassrls FROM pg_roles WHERE rolname=current_user)
                """)).one()
                if (database != "fashionstore_test" or current_role != role
                        or port != 55432 or bypass_rls is not bypass):
                    return False
                if conexion.execute(text("SELECT to_regclass('public.empresa')")).scalar_one() is None:
                    return False
        except SQLAlchemyError:
            return False
        finally:
            motor.dispose()
    return True


pytestmark = [
    pytest.mark.skipif(
        not _base_disponible(),
        reason="Requiere PostgreSQL de pruebas (55432) y scripts/seed_demo.py"),
]


# ---------------------------------------------------------------------------
# Infraestructura
# ---------------------------------------------------------------------------

def _configuracion(multitenant_modules: str = "cu09,sync,saas") -> Settings:
    return Settings(
        database_url=SecretStr(APP_URL),
        platform_database_url=SecretStr(PLATFORM_URL),
        cliente_rol_id="E5-CLI",
        environment="test",
        multitenant_enabled=True,
        multitenant_modules=multitenant_modules,
        demo_mode=True,
        password_storage="argon2id",
        payments_provider="mock",
        recovery_delivery="disabled",
        geocoding_provider="disabled",
    )


@pytest.fixture(scope="module")
def credenciales() -> dict:
    return json.loads(CREDENCIALES.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def client():
    with TestClient(create_app(_configuracion()), raise_server_exceptions=False) as cliente:
        cliente.headers.update(CABECERAS)
        yield cliente


@pytest.fixture
def cu07_client():
    with TestClient(create_app(_configuracion("cu07")), raise_server_exceptions=False) as cliente:
        cliente.headers.update(CABECERAS)
        yield cliente


@pytest.fixture(scope="module")
def plataforma():
    motor = create_engine(PLATFORM_URL)
    yield motor
    motor.dispose()


@pytest.fixture(autouse=True)
def limpiar_efectos_venta_propios(plataforma, request):
    """Revierte únicamente las operaciones y ventas creadas por cada prueba."""
    with plataforma.connect() as conexion:
        ventas_antes = set(conexion.execute(
            text("SELECT idEmp, nroVenta FROM venta")).all())
        operaciones_antes = set(conexion.execute(
            text("SELECT idEmp, idOperacion FROM operacion_sync")).all())
    yield
    with plataforma.begin() as conexion:
        cu23_fixture = request.node.funcargs.get("cu23_sales")
        cu23_sale_ids = set(cu23_fixture["sale_ids"]) if cu23_fixture is not None else set()
        ventas_nuevas = [row for row in conexion.execute(
            text('SELECT idEmp AS "idEmp", nroVenta AS "nroVenta", '
                 'idOperacion AS "idOperacion", nroSuc AS "nroSuc", origen FROM venta')).all()
            if (row.idEmp, row.nroVenta) not in ventas_antes
            and row.nroVenta not in cu23_sale_ids]
        operaciones_nuevas = [row for row in conexion.execute(
            text('SELECT idEmp AS "idEmp", idOperacion AS "idOperacion" FROM operacion_sync')).all()
            if (row.idEmp, row.idOperacion) not in operaciones_antes]

        for venta in ventas_nuevas:
            detalles = conexion.execute(text("""
                SELECT idVar AS "idVar", cantidad FROM detalleVenta
                WHERE idEmp=:idemp AND nroVenta=:venta
            """), {"idemp": venta.idEmp, "venta": venta.nroVenta}).all()
            for detalle in detalles:
                conexion.execute(text("""
                    UPDATE inventario SET stock=stock + :cantidad,
                                           cantDisp=cantDisp + :cantidad
                    WHERE idEmp=:idemp AND nroSuc=:sucursal AND idVar=:variante
                """), {"idemp": venta.idEmp, "sucursal": venta.nroSuc,
                      "variante": detalle.idVar, "cantidad": detalle.cantidad})
                conexion.execute(text("""
                    DELETE FROM movimiento_inv m USING inventario i
                    WHERE m.idEmp=:idemp AND m.nroInv=i.nroInv AND i.idEmp=m.idEmp
                      AND i.nroSuc=:sucursal AND i.idVar=:variante
                      AND m.motivo=:motivo
                """), {"idemp": venta.idEmp, "sucursal": venta.nroSuc,
                      "variante": detalle.idVar,
                      "motivo": f"Venta {venta.origen} #{venta.nroVenta}"})
            conexion.execute(text(
                "DELETE FROM pago WHERE idEmp=:idemp AND nroVenta=:venta"),
                {"idemp": venta.idEmp, "venta": venta.nroVenta})

        operaciones = {(row.idEmp, row.idOperacion) for row in operaciones_nuevas}
        operaciones.update((row.idEmp, row.idOperacion) for row in ventas_nuevas if row.idOperacion)
        for idemp, operacion in operaciones:
            conexion.execute(text("""
                DELETE FROM bitacora
                WHERE idEmp=:idemp AND detalles->>'idOperacion'=:operacion
            """), {"idemp": idemp, "operacion": str(operacion)})
            conexion.execute(text("""
                DELETE FROM operacion_sync WHERE idEmp=:idemp AND idOperacion=:operacion
            """), {"idemp": idemp, "operacion": operacion})
        for venta in ventas_nuevas:
            conexion.execute(text(
                "DELETE FROM venta WHERE idEmp=:idemp AND nroVenta=:venta"),
                {"idemp": venta.idEmp, "venta": venta.nroVenta})
    inventario = request.node.funcargs.get("inventario_dependencias")
    if inventario is not None:
        with plataforma.begin() as conexion:
            conexion.execute(text("""
                DELETE FROM movimiento_inv
                WHERE idEmp=:idemp AND nroInv IN (
                    SELECT nroInv FROM inventario
                    WHERE idEmp=:idemp AND idVar=:variante
                )
            """), {"idemp": inventario["idEmp"], "variante": inventario["idVariante"]})
            conexion.execute(text("""
                DELETE FROM inventario WHERE idEmp=:idemp AND idVar=:variante
            """), {"idemp": inventario["idEmp"], "variante": inventario["idVariante"]})
            conexion.execute(text("""
                DELETE FROM varianteProd
                WHERE idEmp=:idemp AND idVariante=:variante
            """), {"idemp": inventario["idEmp"], "variante": inventario["idVariante"]})
            conexion.execute(text("""
                DELETE FROM producto WHERE idEmp=:idemp AND idProd=:producto
            """), {"idemp": inventario["idEmp"], "producto": inventario["idProducto"]})


@pytest.fixture
def clientes_temporales_cu07(plataforma):
    creados: list[tuple[int, str]] = []
    yield creados
    with plataforma.begin() as conexion:
        for idemp, user_id in creados:
            conexion.execute(text("""
                DELETE FROM bitacora
                WHERE idEmp=:idemp
                  AND (detalles->>'idUsuario'=:user_id OR usuario_id=:user_id)
            """), {"idemp": idemp, "user_id": user_id})
            conexion.execute(text("""
                DELETE FROM recuperacion_contrasena
                WHERE idEmp=:idemp AND usuario_id=:user_id
            """), {"idemp": idemp, "user_id": user_id})
            conexion.execute(text("""
                DELETE FROM cliente WHERE idEmp=:idemp AND idUsuario=:user_id
            """), {"idemp": idemp, "user_id": user_id})
            conexion.execute(text("""
                DELETE FROM usuario WHERE idEmp=:idemp AND idUsuario=:user_id
            """), {"idemp": idemp, "user_id": user_id})


@pytest.fixture
def inventario_dependencias(plataforma, credenciales):
    codigo = "kosy-pantalones"
    empresa = next(row for row in credenciales["empresas"] if row["codigo"] == codigo)
    cajero_email = empresa["usuarios"]["cajero"]["correo"].strip().lower()
    id_producto = f"TC24{uuid4().hex[:16]}"
    id_variante = f"T{uuid4().hex[:12]}"
    sku = f"T{uuid4().hex[:20]}"
    with plataforma.begin() as conexion:
        sucursal = conexion.execute(text("""
            SELECT e.idEmp AS "idEmp", em.idUsuario AS cajero, s.nro AS sucursal
            FROM empresa e
            JOIN usuario u ON u.idEmp=e.idEmp AND lower(btrim(u.correo))=:correo
            JOIN empleado em ON em.idEmp=u.idEmp AND em.idUsuario=u.idUsuario
            JOIN sucursal s ON s.idEmp=em.idEmp AND s.nro=em.nroSuc
            WHERE e.codigo=:codigo AND s.estado='activo'
        """), {"codigo": codigo, "correo": cajero_email}).mappings().first()
        if sucursal is None:
            pytest.fail(f"Fixture SaaS mal preparado: no se encontró sucursal activa de {codigo}")
        conexion.execute(text("SELECT set_config('app.idemp', :idemp, true)"),
                         {"idemp": str(sucursal["idEmp"])})
        template = conexion.execute(text("""
            SELECT p.idCat AS "idCat", p.idMarca AS "idMarca", p.idCol AS "idCol",
                   p.idProv AS "idProv", v.idTalla AS "idTalla", v.precio
            FROM producto p
            JOIN varianteProd v ON v.idEmp=p.idEmp AND v.idProd=p.idProd
            WHERE p.idEmp=:idemp AND p.estado='activo' AND v.estado='activo'
            ORDER BY p.idProd, v.idVariante LIMIT 1
        """), {"idemp": sucursal["idEmp"]}).mappings().first()
        if template is None:
            pytest.fail(f"Fixture SaaS mal preparado: {codigo} no tiene referencias de catálogo")
        inventario_id = conexion.scalar(text("SELECT coalesce(max(nroInv), 0) + 1 FROM inventario"))
        conexion.execute(text("""
            INSERT INTO producto(idProd, descripcion, estado, idPromo, idCat,
                                 idMarca, idCol, idProv, idEmp)
            VALUES (:producto, 'Fixture aislada test_dependencias', 'activo', NULL,
                    :categoria, :marca, :coleccion, :proveedor, :idemp)
        """), {"producto": id_producto, "categoria": template["idCat"],
              "marca": template["idMarca"], "coleccion": template["idCol"],
              "proveedor": template["idProv"], "idemp": sucursal["idEmp"]})
        conexion.execute(text("""
            INSERT INTO varianteProd(idVariante, sku, precio, estado, idTalla,
                                     idProd, idEmp)
            VALUES (:variante, :sku, :precio, 'activo', :talla, :producto, :idemp)
        """), {"variante": id_variante, "sku": sku, "precio": template["precio"],
              "talla": template["idTalla"], "producto": id_producto,
              "idemp": sucursal["idEmp"]})
        conexion.execute(text("""
            INSERT INTO inventario(nroInv, stock, cantDisp, nroSuc, idVar, idEmp)
            VALUES (:inventario, 20, 20, :sucursal, :variante, :idemp)
        """), {"inventario": inventario_id, "sucursal": sucursal["sucursal"], "variante": id_variante,
              "idemp": sucursal["idEmp"]})
    yield {
        "idEmp": sucursal["idEmp"], "sucursal": sucursal["sucursal"],
        "cajero": sucursal["cajero"], "idProducto": id_producto,
        "idVariante": id_variante, "precio": template["precio"], "stock": 20,
    }


@pytest.fixture(scope="module")
def empresa_por_codigo(credenciales) -> dict:
    return {empresa["codigo"]: empresa for empresa in credenciales["empresas"]}


@pytest.fixture(scope="module")
def datos(plataforma):
    """Datos reales del tenant, leídos con el rol de plataforma (BYPASSRLS)."""
    cache: dict[str, dict] = {}

    def obtener(codigo: str) -> dict:
        if codigo not in cache:
            with plataforma.connect() as conexion:
                fila = conexion.execute(text("""
                    SELECT e.idEmp AS "idEmp", s.nro AS sucursal,
                           v.idVariante AS "idVariante", v.precio,
                           i.cantDisp AS "cantDisp", em.idUsuario AS cajero
                    FROM empresa e
                    JOIN empleado em ON em.idEmp = e.idEmp
                    JOIN sucursal s ON s.idEmp = e.idEmp AND s.nro = em.nroSuc
                    JOIN inventario i ON i.idEmp = e.idEmp AND i.nroSuc = s.nro
                    JOIN varianteProd v ON v.idEmp = i.idEmp AND v.idVariante = i.idVar
                    WHERE e.codigo = :codigo AND i.cantDisp > 0
                    ORDER BY i.cantDisp DESC, em.cargo, v.idVariante
                    LIMIT 1
                """), {"codigo": codigo}).mappings().first()
            if fila is None:
                pytest.fail(f"Fixture SaaS mal preparado: {codigo} no tiene stock en la sucursal asignada al empleado")
            cache[codigo] = dict(fila)
        return cache[codigo]

    return obtener


def _entrar_empresa(client, credenciales, codigo: str, rol: str):
    credencial = next(e for e in credenciales["empresas"] if e["codigo"] == codigo)["usuarios"][rol]
    client.cookies.clear()
    return client.post("/api/auth/login", json={
        "empresa": codigo, "correo": credencial["correo"],
        "contrasena": credencial["contrasena"],
    })


def _entrar_plataforma(client, credenciales):
    client.cookies.clear()
    return client.post("/api/auth/login/saas", json={
        "correo": credenciales["plataforma"]["correo"],
        "contrasena": credenciales["plataforma"]["contrasena"],
    })


@pytest.fixture
def cu08_client():
    with TestClient(create_app(_configuracion("cu08,saas")), raise_server_exceptions=False) as cliente:
        cliente.headers.update(CABECERAS)
        yield cliente


@pytest.fixture
def eventos_cu08(plataforma, credenciales):
    """Synthetic events owned by this run; never delete pre-existing audit rows."""
    marker = "cu08-" + uuid4().hex
    events = {}
    ids = []
    with plataforma.begin() as connection:
        for code in ("manaco", "urbanwomen"):
            company = next(e for e in credenciales["empresas"] if e["codigo"] == code)
            actor, tenant = connection.execute(text("""
                SELECT u.idusuario, u.idemp FROM usuario u JOIN empresa e ON e.idemp=u.idemp
                WHERE e.codigo=:code AND lower(btrim(u.correo))=:email
            """), {"code": code, "email": company["usuarios"]["admin"]["correo"].lower()}).one()
            events[code] = {"actor": actor, "tenant": tenant, "ids": []}
            for _ in range(3):
                event_id = connection.scalar(text("""
                    INSERT INTO bitacora(idemp, usuario_id, accion, fecha, detalles, ip)
                    VALUES (:tenant, :actor, 'rol_creado', '2097-01-01T12:00:00Z',
                            CAST(:details AS jsonb), '127.0.0.1') RETURNING id
                """), {"tenant": tenant, "actor": actor, "details": json.dumps({
                    "resultado": "exito", "prueba": marker, "contrasena": "cu08-secret",
                    "token": "cu08-secret", "idUsuario": "foreign-reference"})})
                events[code]["ids"].append(str(event_id))
                ids.append(event_id)
        platform_id = connection.scalar(text("""
            INSERT INTO bitacora(idemp, usuario_id, accion, fecha, detalles)
            VALUES (NULL, NULL, 'rol_creado', '2097-01-01T12:00:00Z',
                    CAST(:details AS jsonb)) RETURNING id
        """), {"details": json.dumps({"prueba": marker})})
        ids.append(platform_id)
        events["platform"] = str(platform_id)
    try:
        yield events
    finally:
        with plataforma.begin() as connection:
            for event_id in ids:
                connection.execute(text("""
                    DELETE FROM bitacora WHERE id=:id AND detalles->>'prueba'=:marker
                """), {"id": event_id, "marker": marker})


@pytest.mark.parametrize("code,other", [("manaco", "urbanwomen"), ("urbanwomen", "manaco")])
def test_cu08_tenant_filters_pagination_secrets_and_platform_separation(
        cu08_client, credenciales, eventos_cu08, code, other):
    client = cu08_client
    own, foreign = eventos_cu08[code], eventos_cu08[other]
    assert _entrar_empresa(client, credenciales, code, "admin").status_code == 200
    filters = {"accion": "rol_creado", "usuario_id": own["actor"],
               "desde": "2097-01-01T00:00:00Z", "hasta": "2097-01-02T00:00:00Z", "limit": 2}
    response = client.get("/api/admin/bitacora", params=filters)
    assert response.status_code == 200
    page = response.json()
    assert page["total"] == 3 and page["offset"] == 0 and page["limit"] == 2
    assert [r["id"] for r in page["items"]] == list(reversed(own["ids"]))[:2]
    second = client.get("/api/admin/bitacora", params=filters | {"offset": 2}).json()
    assert second["total"] == 3 and [r["id"] for r in second["items"]] == own["ids"][:1]
    empty = client.get("/api/admin/bitacora", params=filters | {"offset": 100}).json()
    assert empty["total"] == 3 and empty["items"] == []
    for event_id in own["ids"]:
        detail = client.get("/api/admin/bitacora/" + event_id)
        assert detail.status_code == 200
        assert detail.json()["usuario_id"] == own["actor"]
        assert detail.json()["detalles"] == {"resultado": "exito"}
        assert "cu08-secret" not in detail.text and "foreign-reference" not in detail.text
    for event_id in foreign["ids"] + [eventos_cu08["platform"]]:
        assert client.get("/api/admin/bitacora/" + event_id).status_code == 404
    assert client.get("/api/admin/bitacora", params=filters | {"usuario_id": foreign["actor"]}).json()["total"] == 0
    # No actor filter: count still excludes both the other tenant and platform.
    assert client.get("/api/admin/bitacora", params={k: v for k, v in filters.items() if k != "usuario_id"}).json()["total"] == 3
    assert client.get("/api/admin/bitacora", params={"idemp": foreign["tenant"]}).status_code == 422
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert client.request(method, "/api/admin/bitacora/" + own["ids"][0]).status_code == 405
    assert client.get("/api/saas/auditoria").status_code == 403
    assert _entrar_plataforma(client, credenciales).status_code == 200
    assert client.get("/api/admin/bitacora").status_code == 401
    platform = client.get("/api/saas/auditoria?limite=500")
    assert platform.status_code == 200
    platform_ids = {str(row["id"]) for row in platform.json()}
    assert eventos_cu08["platform"] in platform_ids
    assert not platform_ids.intersection(own["ids"] + foreign["ids"])


@pytest.mark.parametrize("role,status", [(None, 401), ("encargado", 403), ("cajero", 403), ("cliente", 403)])
def test_cu08_requires_exact_permission(cu08_client, credenciales, role, status):
    if role is not None:
        assert _entrar_empresa(cu08_client, credenciales, "manaco", role).status_code == 200
    for route in ("/api/admin/bitacora", "/api/admin/bitacora/1"):
        assert cu08_client.get(route).status_code == status


def test_cu08_closed_without_explicit_allowlist(client, credenciales):
    assert _entrar_empresa(client, credenciales, "manaco", "admin").status_code == 200
    assert client.get("/api/admin/bitacora").status_code == 503


def _cliente_cu07(correo: str) -> dict:
    return {
        "ci": f"CU07-{uuid4().hex[:12]}",
        "nombres": "Cliente",
        "apellidoPat": "Prueba",
        "apellidoMat": "Tenant",
        "sexo": "F",
        "correo": correo,
        "telefono": "70000000",
        "direccion": "Dirección temporal de pruebas",
        "fechaNac": "2000-01-01",
        "contrasena": "FashionStore-CU07-Test-2026!",
    }


@pytest.fixture
def cu10_client():
    with TestClient(create_app(_configuracion("cu10")), raise_server_exceptions=False) as cliente:
        cliente.headers.update(CABECERAS)
        yield cliente


@pytest.fixture
def catalogos_cu10(plataforma):
    """Two owned catalogs, no seed/reset and no changes to historical inventory."""
    catalogs = {}
    cleanup = []
    marker = "C10" + uuid4().hex[:12]
    committed = False
    try:
        with plataforma.begin() as conn:
            for code in ("manaco", "urbanwomen"):
                tenant = conn.scalar(text("SELECT idemp FROM empresa WHERE codigo=:code"), {"code": code})
                conn.execute(text("SELECT set_config('app.idemp', :tenant, true)"), {"tenant": str(tenant)})
                owned = {}
                cleanup.append((tenant, owned))
                def insert(table, key, columns, values):
                    # Demo data has explicit IDs and some identity sequences lag.
                    # Allocate owned fixture IDs under a table lock, never reset
                    # shared sequences or rewrite existing rows to prepare tests.
                    conn.execute(text(f"LOCK TABLE {table} IN SHARE ROW EXCLUSIVE MODE"))
                    row_id = conn.scalar(text(f"SELECT coalesce(max({key}),0)+1 FROM {table}"))
                    conn.execute(text(f"INSERT INTO {table}({key},idemp,{columns}) VALUES (:id,:tenant,{values})"),
                                 {"id": row_id, "tenant": tenant, "name": marker})
                    owned.setdefault((table, key), []).append(row_id)
                    return row_id
                cat = insert("categoria", "idcat", "descripcion", ":name")
                brand = insert("marca", "idmarca", "nombre,estado", ":name,'activo'")
                inactive_brand = insert("marca", "idmarca", "nombre,estado", ":name,'inactivo'")
                col = insert("coleccion", "idcol", "descripcion", ":name")
                season = insert("temporada", "idtemp", "nombre,fechaini,fechafin,estado", ":name,CURRENT_DATE,CURRENT_DATE+1,'activo'")
                conn.execute(text("INSERT INTO tempcoleccion(idemp,idcol,idtemp) VALUES (:tenant,:col,:season)"),
                             {"tenant": tenant, "col": col, "season": season})
                talla = insert("talla", "idtalla", "descripcion", "'C10-M'")
                color = insert("color", "idcolor", "descripcion,hex", ":name,'#112233'")
                provider = insert("proveedor", "idprov", "nombre,correo,direccion,telefono", ":name,'cu10@example.com','Prueba CU10','0'")
                promo = insert("promocion", "idpromo", "nombre,tipodescuento,valordescuento,fechaini,fechafin,estado",
                               ":name,'porcentaje',10,CURRENT_DATE,CURRENT_DATE+1,'inactivo'")
                city, branch = conn.execute(text("SELECT idciud,nro FROM sucursal WHERE idemp=:tenant AND estado='activo' ORDER BY nro LIMIT 1"), {"tenant": tenant}).one()
                closed = insert("sucursal", "nro", "nombre,direccion,estado,idciud", f":name,'Prueba CU10','inactivo',{int(city)}")
                products, variants = [], []
                for index, state in enumerate(("activo", "activo", "inactivo", "activo")):
                    pid = f"{marker}-{tenant}-{index}"
                    vid = "T" + uuid4().hex[:14]
                    products.append(pid); variants.append(vid)
                    conn.execute(text("""INSERT INTO producto(idemp,idprod,descripcion,estado,idcat,idmarca,idcol,idprov,idpromo)
                        VALUES (:tenant,:pid,:name,:state,:cat,:brand,:col,:provider,:promo)"""),
                        {"tenant": tenant, "pid": pid, "name": f"{marker} prenda {index}", "state": state,
                         "cat": cat, "brand": brand, "col": col, "provider": provider, "promo": promo})
                    owned.setdefault(("producto", "idprod"), []).append(pid)
                    conn.execute(text("""INSERT INTO varianteprod(idemp,idvariante,sku,precio,estado,idtalla,idprod)
                        VALUES (:tenant,:vid,:vid,:price,'activo',:talla,:pid)"""),
                        {"tenant": tenant, "vid": vid, "price": (index + 1) * 111, "talla": talla, "pid": pid})
                    owned.setdefault(("varianteprod", "idvariante"), []).append(vid)
                    conn.execute(text("INSERT INTO variantecolors(idemp,idvar,idcolor) VALUES (:tenant,:vid,:color)"),
                                 {"tenant": tenant, "vid": vid, "color": color})
                    conn.execute(text("LOCK TABLE inventario IN SHARE ROW EXCLUSIVE MODE"))
                    inv = conn.scalar(text("SELECT coalesce(max(nroinv),0)+1 FROM inventario"))
                    conn.execute(text("""INSERT INTO inventario(nroinv,idemp,stock,cantdisp,nrosuc,idvar)
                        VALUES (:inv,:tenant,7,7,:branch,:vid)"""),
                        {"inv": inv, "tenant": tenant, "branch": closed if index == 3 else branch, "vid": vid})
                    owned.setdefault(("inventario", "nroinv"), []).append(inv)
                inactive_variant = "T" + uuid4().hex[:14]
                conn.execute(text("""INSERT INTO varianteprod(idemp,idvariante,sku,precio,estado,idtalla,idprod)
                    VALUES (:tenant,:vid,:vid,9999,'inactivo',:talla,:pid)"""),
                    {"tenant": tenant, "vid": inactive_variant, "talla": talla, "pid": products[0]})
                owned[("varianteprod", "idvariante")].append(inactive_variant)
                catalogs[code] = dict(tenant=tenant, marker=marker, cat=cat, brand=brand, col=col,
                    season=season, talla=talla, color=color, branch=branch, closed=closed,
                    inactive_brand=inactive_brand, products=products, variants=variants, provider=provider,
                    inactive_variant=inactive_variant)
        committed = True
        yield catalogs
    finally:
        if committed:
            with plataforma.begin() as conn:
                for tenant, owned in cleanup:
                    conn.execute(text("SELECT set_config('app.idemp', :tenant, true)"), {"tenant": str(tenant)})
                    conn.execute(
                        text("DELETE FROM politica_devolucion WHERE idemp=:tenant AND idprod=ANY(:ids)"),
                        {"tenant": tenant, "ids": owned.get(("producto", "idprod"), [])},
                    )
                    conn.execute(text("DELETE FROM variantecolors WHERE idemp=:tenant AND idvar=ANY(:ids)"),
                                 {"tenant": tenant, "ids": owned.get(("varianteprod", "idvariante"), [])})
                    conn.execute(text("DELETE FROM tempcoleccion WHERE idemp=:tenant AND idcol=ANY(:ids)"),
                                 {"tenant": tenant, "ids": owned.get(("coleccion", "idcol"), [])})
                    for table, key in (("inventario", "nroinv"), ("varianteprod", "idvariante"), ("producto", "idprod"),
                                       ("sucursal", "nro"), ("promocion", "idpromo"), ("proveedor", "idprov"),
                                       ("temporada", "idtemp"), ("coleccion", "idcol"), ("marca", "idmarca"),
                                       ("categoria", "idcat"), ("talla", "idtalla"), ("color", "idcolor")):
                        conn.execute(text(f"DELETE FROM {table} WHERE idemp=:tenant AND {key}=ANY(:ids)"),
                                     {"tenant": tenant, "ids": owned.get((table, key), [])})


@pytest.mark.parametrize("code,other", [("manaco", "urbanwomen"), ("urbanwomen", "manaco")])
def test_cu10_catalogo_filtros_detalle_disponibilidad_y_tenant(
        cu10_client, credenciales, catalogos_cu10, plataforma, code, other):
    client = cu10_client
    own, foreign = catalogos_cu10[code], catalogos_cu10[other]
    assert _entrar_empresa(client, credenciales, code, "cliente").status_code == 200
    with plataforma.connect() as conn:
        before = conn.scalar(text("SELECT count(*) FROM bitacora"))
    base = "/api/catalogo/productos"
    filters = {"q": own["marker"], "limit": 1, "sort": "precio_asc"}
    page = client.get(base, params=filters)
    assert page.status_code == 200 and page.json()["total"] == 3
    assert page.json()["items"][0]["idProd"] == own["products"][0]
    second = client.get(base, params=filters | {"offset": 1}).json()
    assert second["total"] == 3 and second["items"][0]["idProd"] == own["products"][1]
    assert client.get(base, params=filters | {"offset": 99}).json()["items"] == []
    assert client.get(base, params=filters | {"offset": 99}).json()["total"] == 3
    for key, ref in (("idCat", "cat"), ("idMarca", "brand"), ("idCol", "col"),
                     ("idTemp", "season"), ("idTalla", "talla"), ("idColor", "color")):
        assert client.get(base, params=filters | {key: own[ref]}).json()["total"] == 3
        assert client.get(base, params=filters | {key: foreign[ref]}).json()["total"] == 0
    assert client.get(base, params=filters | {"minPrecio": 200, "maxPrecio": 230}).json()["total"] == 1
    assert client.get(base, params=filters | {"soloDisponibles": True}).json()["total"] == 2
    assert client.get(base, params=filters | {"minPrecio": 9999}).json()["total"] == 0
    for pid in foreign["products"] + [own["products"][2]]:
        assert client.get(base + "/" + pid).status_code == 404
    for vid in (foreign["variants"][0], own["inactive_variant"], own["variants"][2]):
        assert client.get("/api/catalogo/variantes/" + vid).status_code == 404
    detail = client.get(base + "/" + own["products"][0]).json()
    assert detail["promocion"] is None
    assert [v["idVariante"] for v in detail["variantes"]] == own["variants"][:1]
    assert detail["variantes"][0]["talla"]["idTalla"] == own["talla"]
    assert detail["variantes"][0]["colores"][0]["idColor"] == own["color"]
    assert {r["nroSuc"] for r in detail["disponibilidad"]} == {own["branch"]}
    assert sum(r["cantDisp"] for r in detail["disponibilidad"]) == 7
    assert client.get(base + "/" + own["products"][3]).json()["disponibilidad"] == []
    closed = client.get(base, params=filters | {"idCat": own["cat"], "limit": 100}).json()
    assert next(p for p in closed["items"] if p["idProd"] == own["products"][3])["disponible"] is False
    for group, ref in (("categorias", "cat"), ("marcas", "brand"), ("colecciones", "col"),
                       ("temporadas", "season"), ("tallas", "talla"), ("colores", "color")):
        facets = client.get("/api/catalogo/" + group).json()
        ids = {r["id"] for r in facets["items"]}
        assert own[ref] in ids and foreign[ref] not in ids
        assert facets["total"] == len(ids)
        if group == "marcas":
            assert own["inactive_brand"] not in ids
    assert client.get("/api/catalogo/marcas", params={"idCat": foreign["cat"]}).json()["total"] == 0
    for route in (base, base + "/" + own["products"][0], "/api/catalogo/categorias"):
        assert client.get(route, params={"empresa": other}).status_code == 422
        assert client.get(route, headers={"X-Empresa": other}).status_code == 422
    assert client.get(base, params={"nroSuc": foreign["branch"]}).status_code == 422
    for pending in ("/api/cliente/reservas", "/api/cliente/carrito", "/api/cliente/sucursales"):
        assert client.get(pending).status_code == 503
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert client.request(method, base).status_code == 405
    with plataforma.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM bitacora")) == before
        assert conn.scalar(text("SELECT cantdisp FROM inventario WHERE idemp=:tenant AND idvar=:vid"),
                           {"tenant": own["tenant"], "vid": own["variants"][0]}) == 7


def test_cu10_sesion_obligatoria_empresa_invalida_y_bearer(cu10_client, credenciales):
    client = cu10_client
    base = "/api/catalogo/productos"
    for params in ({}, {"empresa": "manaco"}, {"idEmp": 5}):
        assert client.get(base, params=params).status_code == 401
    company = next(e for e in credenciales["empresas"] if e["codigo"] == "manaco")
    customer = company["usuarios"]["cliente"]
    for company_code in (None, "empresa-cu10-inexistente"):
        # Only the login contract, never spread demo metadata into the request.
        # Keep the request outside the assertion so pytest cannot dump secrets.
        rejected = client.post("/api/auth/login/cliente", json={"empresa": company_code,
            "correo": customer["correo"], "contrasena": customer["contrasena"]})
        assert rejected.status_code == 401
    assert _entrar_plataforma(client, credenciales).status_code == 200
    assert client.get(base).status_code == 401
    assert _entrar_empresa(client, credenciales, "manaco", "admin").status_code == 200
    assert client.get(base).status_code == 403
    assert _entrar_empresa(client, credenciales, "manaco", "cliente").status_code == 200
    credential = client.cookies.get(client.app.state.settings.cookie_name)
    client.cookies.clear()
    assert client.get(base, headers={"Authorization": "Bearer " + credential}).status_code == 200


def test_cu10_cerrado_sin_allowlist(client, credenciales):
    assert _entrar_empresa(client, credenciales, "manaco", "cliente").status_code == 200
    assert client.get("/api/catalogo/productos").status_code == 503


@pytest.fixture
def reservas_clientes(plataforma, credenciales, catalogos_cu10):
    """Own clients/cart/reservations; cleanup never deletes historical audit."""
    accounts = {}
    committed = False
    with plataforma.begin() as conn:
        for code, catalog in catalogos_cu10.items():
            original = next(e for e in credenciales["empresas"] if e["codigo"] == code)["usuarios"]["cliente"]
            accounts[code] = []
            for _ in range(2):
                user = "RC" + uuid4().hex
                email = user.lower() + "@example.com"
                conn.execute(text("SELECT set_config('app.idemp', :tenant, true)"), {"tenant": str(catalog["tenant"])})
                conn.execute(text("""INSERT INTO usuario(idusuario,ci,nombre,apellidopat,apellidomat,
                    sexo,correo,telefono,direccion,contrasena,fechanac,tipo,nrorol,idemp)
                    SELECT :user,:ci,'Reserva carrito','Prueba','Tenant',sexo,:email,telefono,direccion,
                        contrasena,fechanac,'C',nrorol,idemp FROM usuario
                    WHERE idemp=:tenant AND lower(btrim(correo))=:original"""),
                    {"user": user, "ci": user[:16], "email": email, "tenant": catalog["tenant"],
                     "original": original["correo"].lower()})
                conn.execute(text("INSERT INTO cliente(idusuario,cod_cl,idemp) VALUES (:user,:cod,:tenant)"),
                             {"user": user, "cod": uuid4().hex[:10], "tenant": catalog["tenant"]})
                accounts[code].append(dict(user=user, email=email, password=original["contrasena"],
                                           code=code, tenant=catalog["tenant"]))
    committed = True
    try:
        yield accounts
    finally:
        if committed:
            with plataforma.begin() as conn:
                for company in accounts.values():
                    for account in company:
                        params = {"tenant": account["tenant"], "user": account["user"]}
                        conn.execute(text("SELECT set_config('app.idemp', :tenant, true)"), {"tenant": str(account["tenant"])})
                        for nro in conn.scalars(text("SELECT nroreserva FROM reserva WHERE idemp=:tenant "
                                                     "AND idusuariocl=:user AND estado IN ('pendiente','confirmada')"), params).all():
                            conn.execute(text("CALL sp_cancelar_reserva(CAST(:tenant AS integer), CAST(:nro AS integer))"),
                                         {"tenant": account["tenant"], "nro": nro})
                        conn.execute(text("DELETE FROM operacion_sync WHERE idemp=:tenant AND idusuario=:user"), params)
                        own_sales=conn.scalars(text('SELECT nroventa FROM venta WHERE idemp=:tenant AND idusuariocl=:user'),params).all()
                        for sale_id in own_sales:
                            conn.execute(text('DELETE FROM movimiento_inv WHERE idemp=:tenant AND motivo=:motive'),
                                         {'tenant':account['tenant'],'motive':f'venta digital nro {sale_id}'})
                        conn.execute(text('DELETE FROM pago WHERE idemp=:tenant AND nroventa IN (SELECT nroventa FROM venta WHERE idemp=:tenant AND idusuariocl=:user)'),params)
                        conn.execute(text('DELETE FROM detalleventa WHERE idemp=:tenant AND nroventa IN (SELECT nroventa FROM venta WHERE idemp=:tenant AND idusuariocl=:user)'),params)
                        conn.execute(text('DELETE FROM venta WHERE idemp=:tenant AND idusuariocl=:user'),params)
                        conn.execute(text("DELETE FROM detallereserva WHERE idemp=:tenant AND nroreserva IN "
                                          "(SELECT nroreserva FROM reserva WHERE idemp=:tenant AND idusuariocl=:user)"), params)
                        conn.execute(text("DELETE FROM reserva WHERE idemp=:tenant AND idusuariocl=:user"), params)
                        conn.execute(text("DELETE FROM detallecarro WHERE idemp=:tenant AND idcarrito IN "
                                          "(SELECT idcarrito FROM carrito WHERE idemp=:tenant AND idusuariocl=:user)"), params)
                        conn.execute(text("DELETE FROM carrito WHERE idemp=:tenant AND idusuariocl=:user"), params)
                        conn.execute(text("DELETE FROM cliente WHERE idemp=:tenant AND idusuario=:user"), params)
                        conn.execute(text("DELETE FROM usuario WHERE idemp=:tenant AND idusuario=:user"), params)


@pytest.fixture
def cu23_sales(plataforma, catalogos_cu10, reservas_clientes):
    marker = "CU23-" + uuid4().hex
    sale_ids: dict[str, tuple[int, int]] = {}
    baseline = {}
    movement_ids = []
    with plataforma.begin() as connection:
        audit_max = connection.scalar(text("SELECT coalesce(max(id), 0) FROM bitacora"))
        for code, variant_indexes, branch_key in (
            ("manaco", (0, 1), "branch"),
            ("manaco", (3,), "closed"),
            ("urbanwomen", (0,), "branch"),
        ):
            catalog = catalogos_cu10[code]
            tenant, branch_id = catalog["tenant"], catalog[branch_key]
            connection.execute(
                text("SELECT set_config('app.idemp', :tenant, true)"),
                {"tenant": str(tenant)},
            )
            account = reservas_clientes[code][0]
            for variant_index in variant_indexes:
                variant = catalog["variants"][variant_index]
                state = connection.execute(
                    text("""
                        SELECT stock AS "stock", cantDisp AS "cantDisp",
                               stockDanado AS "stockDanado", nroInv AS "nroInv"
                        FROM inventario
                        WHERE idEmp=:tenant AND nroSuc=:branch AND idVar=:variant
                    """),
                    {"tenant": tenant, "branch": branch_id, "variant": variant},
                ).mappings().one()
                baseline[(tenant, branch_id, variant)] = dict(state)
                connection.execute(
                    text("""
                        INSERT INTO politica_devolucion(idEmp, idProd, idVar, dias, porcentaje)
                        VALUES (:tenant, :product, NULL, 30, 100)
                        ON CONFLICT (idEmp, idProd) DO UPDATE
                        SET dias=EXCLUDED.dias, porcentaje=EXCLUDED.porcentaje
                    """),
                    {"tenant": tenant, "product": catalog["products"][variant_index]},
                )

        fixtures = (
            ("parcial", "manaco", 0, 2, "branch"),
            ("positivo", "manaco", 0, 1, "branch"),
            ("negativo", "manaco", 1, 1, "branch"),
            ("extranjera", "urbanwomen", 0, 1, "branch"),
            ("otra_sucursal", "manaco", 3, 1, "closed"),
        )
        for key, code, variant_index, quantity, branch_key in fixtures:
            catalog = catalogos_cu10[code]
            tenant = catalog["tenant"]
            branch_id = catalog[branch_key]
            variant = catalog["variants"][variant_index]
            account = reservas_clientes[code][0]
            connection.execute(
                text("SELECT set_config('app.idemp', :tenant, true)"),
                {"tenant": str(tenant)},
            )
            connection.execute(text("LOCK TABLE venta IN SHARE ROW EXCLUSIVE MODE"))
            sale_id = connection.scalar(
                text("SELECT coalesce(max(nroVenta), 0) + 1 FROM venta")
            )
            price = Decimal((variant_index + 1) * 111)
            total = price * quantity
            connection.execute(
                text("""
                    INSERT INTO venta(idEmp, nroVenta, fechaHora, total, desc_aplicado,
                                      estado, idUsuarioCl, nroSuc)
                    VALUES (:tenant, :sale, CURRENT_TIMESTAMP, :total, 0,
                            'registrada', :customer, :branch)
                """),
                {
                    "tenant": tenant,
                    "sale": sale_id,
                    "total": total,
                    "customer": account["user"],
                    "branch": branch_id,
                },
            )
            connection.execute(
                text("""
                    INSERT INTO detalleVenta(idEmp, nroVenta, idDetalleVenta, cantidad,
                                             precioUnitario, descuentoUnitario, idVar)
                    VALUES (:tenant, :sale, 1, :quantity, :price, 0, :variant)
                """),
                {
                    "tenant": tenant,
                    "sale": sale_id,
                    "quantity": quantity,
                    "price": price,
                    "variant": variant,
                },
            )
            connection.execute(
                text("""
                    UPDATE inventario
                    SET stock=stock-:quantity, cantDisp=cantDisp-:quantity
                    WHERE idEmp=:tenant AND nroSuc=:branch AND idVar=:variant
                      AND stock>=:quantity AND cantDisp>=:quantity
                """),
                {
                    "tenant": tenant,
                    "branch": branch_id,
                    "variant": variant,
                    "quantity": quantity,
                },
            )
            movement_id = connection.scalar(
                text("""
                    INSERT INTO movimiento_inv(idEmp, tipoMov, cantidad, fecha, motivo, nroInv)
                    SELECT :tenant, 'salida', :quantity, CURRENT_DATE, :reason, nroInv
                    FROM inventario
                    WHERE idEmp=:tenant AND nroSuc=:branch AND idVar=:variant
                    RETURNING idMov
                """),
                {
                    "tenant": tenant,
                    "branch": branch_id,
                    "variant": variant,
                    "quantity": quantity,
                    "reason": f"{marker} venta #{sale_id}",
                },
            )
            movement_ids.append((tenant, movement_id))
            connection.execute(
                text("""
                    INSERT INTO pago(idEmp, metodo, monto, estado, nroVenta)
                    VALUES (:tenant, 'efectivo', :total, 'aprobado', :sale)
                """),
                {"tenant": tenant, "total": total, "sale": sale_id},
            )
            sale_ids[key] = (tenant, sale_id)
    fixture = {
        "marker": marker,
        "sale_ids": [sale_id for _, sale_id in sale_ids.values()],
        "sales": sale_ids,
        "baseline": baseline,
        "movement_ids": movement_ids,
        "customers": {code: accounts[0] for code, accounts in reservas_clientes.items()},
        "catalogs": catalogos_cu10,
    }
    try:
        yield fixture
    finally:
        with plataforma.begin() as connection:
            for tenant in {tenant for tenant, _ in sale_ids.values()}:
                connection.execute(
                    text("SELECT set_config('app.idemp', :tenant, true)"),
                    {"tenant": str(tenant)},
                )
                source_ids = [
                    sale for sale_tenant, sale in sale_ids.values() if sale_tenant == tenant
                ]
                return_ids = connection.scalars(
                    text("""
                        SELECT nroDev FROM devolucion
                        WHERE idEmp=:tenant AND nroVenta=ANY(:sales)
                    """),
                    {"tenant": tenant, "sales": source_ids},
                ).all()
                changes = connection.execute(
                    text("""
                        SELECT nroCambio AS "nroCambio",
                               nroVentaNueva AS "nroVentaNueva"
                        FROM cambio
                        WHERE idEmp=:tenant AND
                          (nroVenta=ANY(:sales) OR nroDev=ANY(:returns))
                    """),
                    {"tenant": tenant, "sales": source_ids, "returns": return_ids},
                ).mappings().all()
                change_ids = [row["nroCambio"] for row in changes]
                all_sale_ids = source_ids + [
                    row["nroVentaNueva"] for row in changes
                    if row["nroVentaNueva"] is not None
                ]
                payment_ids = connection.scalars(
                    text("""
                        SELECT idPago FROM pago
                        WHERE idEmp=:tenant AND nroVenta=ANY(:sales)
                    """),
                    {"tenant": tenant, "sales": all_sale_ids},
                ).all()
                connection.execute(
                    text("""
                        DELETE FROM bitacora
                        WHERE idEmp=:tenant AND id>:audit_max AND (
                          detalles->>'nroDev'=ANY(:returns_text)
                          OR detalles->>'nroCambio'=ANY(:changes_text)
                          OR detalles->>'idReembolso'=ANY(:refunds_text)
                          OR detalles->>'idPago'=ANY(:payments_text)
                        )
                    """),
                    {
                        "tenant": tenant,
                        "audit_max": audit_max,
                        "returns_text": [str(value) for value in return_ids],
                        "changes_text": [str(value) for value in change_ids],
                        "refunds_text": [
                            str(value)
                            for value in connection.scalars(
                                text("""
                                    SELECT idReembolso FROM reembolso
                                    WHERE idEmp=:tenant AND nroDev=ANY(:returns)
                                """),
                                {"tenant": tenant, "returns": return_ids},
                            ).all()
                        ],
                        "payments_text": [str(value) for value in payment_ids],
                    },
                )
                connection.execute(
                    text("DELETE FROM reembolso WHERE idEmp=:tenant AND nroDev=ANY(:returns)"),
                    {"tenant": tenant, "returns": return_ids},
                )
                connection.execute(
                    text("DELETE FROM detalleCambio WHERE idEmp=:tenant AND nroCambio=ANY(:changes)"),
                    {"tenant": tenant, "changes": change_ids},
                )
                connection.execute(
                    text("DELETE FROM cambio WHERE idEmp=:tenant AND nroCambio=ANY(:changes)"),
                    {"tenant": tenant, "changes": change_ids},
                )
                connection.execute(
                    text("DELETE FROM detalleDev WHERE idEmp=:tenant AND nroDev=ANY(:returns)"),
                    {"tenant": tenant, "returns": return_ids},
                )
                connection.execute(
                    text("DELETE FROM devolucion WHERE idEmp=:tenant AND nroDev=ANY(:returns)"),
                    {"tenant": tenant, "returns": return_ids},
                )
                connection.execute(
                    text("DELETE FROM detalleVenta WHERE idEmp=:tenant AND nroVenta=ANY(:sales)"),
                    {"tenant": tenant, "sales": all_sale_ids},
                )
                connection.execute(
                    text("DELETE FROM pago WHERE idEmp=:tenant AND nroVenta=ANY(:sales)"),
                    {"tenant": tenant, "sales": all_sale_ids},
                )
                connection.execute(
                    text("DELETE FROM venta WHERE idEmp=:tenant AND nroVenta=ANY(:sales)"),
                    {"tenant": tenant, "sales": all_sale_ids},
                )
                connection.execute(
                    text("DELETE FROM movimiento_inv WHERE idEmp=:tenant AND idMov=ANY(:ids)"),
                    {
                        "tenant": tenant,
                        "ids": [
                            movement for movement_tenant, movement in movement_ids
                            if movement_tenant == tenant
                        ],
                    },
                )
                movement_reasons = [
                    f"Devolución #{return_id} (apta para venta)"
                    for return_id in return_ids
                ] + [
                    f"Devolución #{return_id} (prenda dañada)"
                    for return_id in return_ids
                ] + [
                    reason
                    for change_id in change_ids
                    for reason in (
                        f"Cambio #{change_id} (prenda devuelta)",
                        f"Cambio #{change_id} (prenda dañada)",
                        f"Cambio #{change_id} (prenda entregada)",
                    )
                ]
                connection.execute(
                    text("DELETE FROM movimiento_inv WHERE idEmp=:tenant AND motivo=ANY(:reasons)"),
                    {"tenant": tenant, "reasons": movement_reasons},
                )
                for (baseline_tenant, branch_id, variant), state in baseline.items():
                    if baseline_tenant == tenant:
                        connection.execute(
                            text("""
                                UPDATE inventario
                                SET stock=:stock, cantDisp=:available, stockDanado=:damaged
                                WHERE idEmp=:tenant AND nroSuc=:branch AND idVar=:variant
                            """),
                            {
                                "tenant": tenant,
                                "branch": branch_id,
                                "variant": variant,
                                "stock": state["stock"],
                                "available": state["cantDisp"],
                                "damaged": state["stockDanado"],
                            },
                        )
                products = {
                    product
                    for catalog in catalogos_cu10.values()
                    if catalog["tenant"] == tenant
                    for product in catalog["products"]
                }
                connection.execute(
                    text("DELETE FROM politica_devolucion WHERE idEmp=:tenant AND idProd=ANY(:products)"),
                    {"tenant": tenant, "products": list(products)},
                )
            for (tenant, branch_id, variant), _ in baseline.items():
                connection.execute(
                    text("DELETE FROM movimiento_inv WHERE idEmp=:tenant AND motivo LIKE :prefix"),
                    {"tenant": tenant, "prefix": marker + "%"},
                )


@pytest.fixture
def cu23_clients():
    with TestClient(create_app(_configuracion("cu10,cu14,cu23")),
                    raise_server_exceptions=False) as staff, \
            TestClient(create_app(_configuracion("cu10,cu14,cu23")),
                       raise_server_exceptions=False) as customer:
        staff.headers.update(CABECERAS)
        customer.headers.update(CABECERAS)
        yield staff, customer


@pytest.fixture
def rc_client():
    with TestClient(create_app(_configuracion("cu10,cu11,cu12,sync")), raise_server_exceptions=False) as client:
        client.headers.update(CABECERAS)
        yield client


def _login_owned(client, account):
    client.cookies.clear()
    response = client.post("/api/auth/login/cliente", json={"empresa": account["code"],
        "correo": account["email"], "contrasena": account["password"]})
    assert response.status_code == 200, response.json()


def _reservation_payload(client, catalog, quantity=2):
    from datetime import timedelta
    ranges = client.get(f"/api/cliente/sucursales/{catalog['branch']}/horarios").json()["rangos"]
    for days in range(1, 15):
        future = date.today() + timedelta(days=days)
        suitable = [row for row in ranges if future.isoweekday() in row["dias"]]
        if suitable or not ranges:
            return {"nroSuc": catalog["branch"], "fechaReserva": future.isoformat(),
                    "horaAtencion": suitable[0]["horaIni"] if suitable else "11:00:00",
                    "items": [{"idVar": catalog["variants"][0], "cantidad": quantity}]}
    raise AssertionError("La fixture no tiene horario elegible")


@pytest.mark.parametrize("code,other", [("manaco", "urbanwomen"), ("urbanwomen", "manaco")])
def test_reservas_carrito_tenant_cliente_totales_y_cancelacion(
        rc_client, reservas_clientes, catalogos_cu10, plataforma, code, other):
    client = rc_client
    own, foreign = catalogos_cu10[code], catalogos_cu10[other]
    a, b = reservas_clientes[code]
    _login_owned(client, a)
    payload = _reservation_payload(client, own)
    created = client.post("/api/cliente/reservas", json=payload)
    assert created.status_code == 201
    nro = created.json()["nroReserva"]
    assert _stock(plataforma, own["tenant"], own["branch"], own["variants"][0]) == 5
    listed = client.get("/api/cliente/reservas?limit=1&estado=pendiente").json()
    assert listed["total"] == 1 and listed["items"][0]["nroReserva"] == nro
    for data in (payload | {"nroSuc": foreign["branch"]},
                 payload | {"items": [{"idVar": foreign["variants"][0], "cantidad": 1}]}):
        assert client.post("/api/cliente/reservas", json=data).status_code == 404
    assert client.get(f"/api/cliente/sucursales/{foreign['branch']}/horarios").status_code == 404
    _login_owned(client, b)
    assert client.get("/api/cliente/reservas").json()["total"] == 0
    assert client.get(f"/api/cliente/reservas/{nro}").status_code == 404
    assert client.patch(f"/api/cliente/reservas/{nro}/cancelar", json={}).status_code == 404
    _login_owned(client, reservas_clientes[other][0])
    assert client.get(f"/api/cliente/reservas/{nro}").status_code == 404
    _login_owned(client, a)
    for _ in range(2):
        assert client.patch(f"/api/cliente/reservas/{nro}/cancelar", json={}).json()["estado"] == "cancelada"
        assert _stock(plataforma, own["tenant"], own["branch"], own["variants"][0]) == 7
    cart_base = "/api/cliente/carrito"
    assert client.get(cart_base).json()["idCarrito"] is None
    assert client.post(cart_base + "/items", json={"idVar": foreign["variants"][0], "cantidad": 1}).status_code == 404
    for quantity in (0, -1, 1.5):
        assert client.post(cart_base + "/items", json={"idVar": own["variants"][0], "cantidad": quantity}).status_code == 422
    assert client.post(cart_base + "/items", json={"idVar": own["variants"][0], "cantidad": 100}).status_code == 409
    added = client.post(cart_base + "/items", json={"idVar": own["variants"][0], "cantidad": 2})
    assert added.status_code == 201
    assert added.json()["cantidadItems"] == 2 and Decimal(added.json()["subtotal"]) == Decimal("222")
    detail_id = added.json()["items"][0]["idDetalleCarro"]
    updated = client.patch(f"{cart_base}/items/{detail_id}", json={"cantidad": 3})
    assert updated.status_code == 200 and Decimal(updated.json()["subtotal"]) == Decimal("333")
    _login_owned(client, b)
    assert client.get(cart_base).json()["idCarrito"] is None
    assert client.patch(f"{cart_base}/items/{detail_id}", json={"cantidad": 1}).status_code == 404
    assert client.delete(f"{cart_base}/items/{detail_id}").status_code == 404
    assert client.get(cart_base, params={"idCarrito": added.json()["idCarrito"]}).status_code == 422
    _login_owned(client, a)
    assert client.delete(f"{cart_base}/items/{detail_id}").json()["cantidadItems"] == 0
    assert _stock(plataforma, own["tenant"], own["branch"], own["variants"][0]) == 7
    for blocked in ("/api/cliente/compras", "/api/cliente/pagos", "/api/admin/caja/ventas"):
        assert client.post(blocked, json={}).status_code == 503


def test_reservas_concurrencia_offline_uuid_y_vencimiento_acotado(
        rc_client, reservas_clientes, catalogos_cu10, plataforma):
    from datetime import timedelta
    a, b = reservas_clientes["manaco"]
    own, foreign = catalogos_cu10["manaco"], catalogos_cu10["urbanwomen"]
    _login_owned(rc_client, a)
    payload = _reservation_payload(rc_client, own, 5)
    def reserve(account):
        with TestClient(create_app(_configuracion("cu11")), raise_server_exceptions=False) as client:
            client.headers.update(CABECERAS)
            _login_owned(client, account)
            response = client.post("/api/cliente/reservas", json=payload)
            return response.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(reserve, (a, b)))
    assert sorted(statuses) == [201, 409]
    assert _stock(plataforma, own["tenant"], own["branch"], own["variants"][0]) == 2
    for account in (a, b):
        _login_owned(rc_client, account)
        for row in rc_client.get("/api/cliente/reservas").json()["items"]:
            assert rc_client.patch(f"/api/cliente/reservas/{row['nroReserva']}/cancelar", json={}).status_code == 200
    assert _stock(plataforma, own["tenant"], own["branch"], own["variants"][0]) == 7
    _login_owned(rc_client, a)
    payload = _reservation_payload(rc_client, own, 2)
    operation = _lote(uuid4(), own["branch"], own["variants"][0], 111, tipo="reserva")
    operation["operaciones"][0]["datos"] = {"fecha": payload["fechaReserva"], "hora": payload["horaAtencion"],
        "lineas": [{"idVariante": own["variants"][0], "cantidad": 2}]}
    first = rc_client.post("/api/sync/lote", json=operation).json()["resultados"][0]
    assert first["estado"] == "sincronizada", first
    nro = first["nroReserva"]
    replay = rc_client.post("/api/sync/lote", json=operation).json()["resultados"][0]
    assert replay["idempotente"] is True and replay["nroReserva"] == nro
    assert _stock(plataforma, own["tenant"], own["branch"], own["variants"][0]) == 5
    _login_owned(rc_client, b)
    denied = rc_client.post("/api/sync/lote", json=operation).json()["resultados"][0]
    assert denied["estado"] == "rechazada" and denied["nroReserva"] is None
    assert operation["operaciones"][0]["id"] not in {str(r["id"]) for r in rc_client.get("/api/sync/operaciones").json()["operaciones"]}
    _login_owned(rc_client, a)
    conflict = json.loads(json.dumps(operation))
    conflict["operaciones"][0]["id"] = str(uuid4())
    conflict["operaciones"][0]["datos"]["lineas"].append({"idVariante": own["variants"][1], "cantidad": 100})
    result = rc_client.post("/api/sync/lote", json=conflict).json()["resultados"][0]
    assert result["estado"] == "conflicto" and result["nroReserva"] is None
    assert _stock(plataforma, own["tenant"], own["branch"], own["variants"][0]) == 5
    assert _stock(plataforma, own["tenant"], own["branch"], own["variants"][1]) == 7
    _login_owned(rc_client, b)
    second = rc_client.post("/api/cliente/reservas", json=payload).json()["nroReserva"]
    with plataforma.begin() as conn:
        conn.execute(text("UPDATE reserva SET fechareserva=CURRENT_DATE-2 WHERE idemp=:tenant AND nroreserva=ANY(:ids)"),
                     {"tenant": own["tenant"], "ids": [nro, second]})
    _login_owned(rc_client, a)
    assert rc_client.get(f"/api/cliente/reservas/{nro}").json()["estado"] == "vencida"
    with plataforma.connect() as conn:
        assert conn.scalar(text("SELECT estado FROM reserva WHERE nroreserva=:nro"), {"nro": second}) == "pendiente"
    assert _stock(plataforma, own["tenant"], own["branch"], own["variants"][0]) == 5
    credential = rc_client.cookies.get(rc_client.app.state.settings.cookie_name)
    rc_client.app.state.access_tokens.revoke(credential)
    expired = rc_client.post("/api/sync/lote", json=operation)
    assert expired.status_code == 401


def test_cu22_preparacion_idempotente_y_atencion_solo_despues_del_pago(
        credenciales, reservas_clientes, catalogos_cu10, plataforma):
    own = catalogos_cu10["manaco"]
    foreign = catalogos_cu10["urbanwomen"]
    customer_account = reservas_clientes["manaco"][0]
    foreign_account = reservas_clientes["urbanwomen"][0]
    reservation_ids = []
    with TestClient(create_app(_configuracion("cu10,cu11,cu22")), raise_server_exceptions=False) as customer:
        customer.headers.update(CABECERAS)
        _login_owned(customer, customer_account)
        created = customer.post("/api/cliente/reservas",
                                json=_reservation_payload(customer, own, 1))
        assert created.status_code == 201, created.text
        nro = created.json()["nroReserva"]
        reservation_ids.append((own["tenant"], nro))
    with TestClient(create_app(_configuracion("cu10,cu11,cu22")), raise_server_exceptions=False) as other:
        other.headers.update(CABECERAS)
        _login_owned(other, foreign_account)
        created = other.post("/api/cliente/reservas",
                             json=_reservation_payload(other, foreign, 1))
        assert created.status_code == 201, created.text
        foreign_nro = created.json()["nroReserva"]
        reservation_ids.append((foreign["tenant"], foreign_nro))
    staff = TestClient(create_app(_configuracion("cu10,cu11,cu22")),
                       raise_server_exceptions=False)
    staff.__enter__()
    staff.headers.update(CABECERAS)
    unauthorized = TestClient(create_app(_configuracion("cu10,cu11,cu22")), raise_server_exceptions=False)
    unauthorized.__enter__()
    unauthorized.headers.update(CABECERAS)
    try:
        login = _entrar_empresa(staff, credenciales, "manaco", "encargado")
        assert login.status_code == 200, login.json()
        assert _entrar_empresa(unauthorized, credenciales, "manaco", "cajero").status_code == 200
        before = _stock(plataforma, own["tenant"], own["branch"], own["variants"][0])
        assert before == 6
        detail = staff.get(f"/api/admin/reservas-sucursal/{nro}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["idCliente"] == customer_account["user"]
        assert detail.json()["estado"] == "pendiente"
        assert staff.get(f"/api/admin/reservas-sucursal/{foreign_nro}").status_code == 404
        assert unauthorized.get("/api/admin/reservas-sucursal").status_code == 403

        confirmed = staff.post(f"/api/admin/reservas-sucursal/{nro}/accion",
                               json={"accion": "confirmar"})
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["estado"] == "confirmada"
        with plataforma.connect() as connection:
            audit_count = connection.scalar(text("""
                SELECT count(*) FROM bitacora
                WHERE idemp=:tenant AND accion='reserva_confirmar'
                  AND detalles->>'nroReserva'=:reservation
            """), {"tenant": own["tenant"], "reservation": str(nro)})
        repeated = staff.post(f"/api/admin/reservas-sucursal/{nro}/accion",
                             json={"accion": "confirmar"})
        assert repeated.status_code == 200 and repeated.json()["estado"] == "confirmada"
        with plataforma.connect() as connection:
            assert connection.scalar(text("""
                SELECT count(*) FROM bitacora
                WHERE idemp=:tenant AND accion='reserva_confirmar'
                  AND detalles->>'nroReserva'=:reservation
            """), {"tenant": own["tenant"], "reservation": str(nro)}) == audit_count == 1

        rejected = staff.post(f"/api/admin/reservas-sucursal/{nro}/accion",
                              json={"accion": "atender"})
        assert rejected.status_code == 409
        assert rejected.json()["error"]["code"] == "reserva_no_cobrada"
        with plataforma.connect() as connection:
            stock, available = connection.execute(text("""
                SELECT stock, cantdisp FROM inventario
                WHERE idemp=:tenant AND nrosuc=:branch AND idvar=:variant
            """), {"tenant": own["tenant"], "branch": own["branch"],
                   "variant": own["variants"][0]}).one()
            state = connection.scalar(text(
                "SELECT estado FROM reserva WHERE idemp=:tenant AND nroreserva=:nro"
            ), {"tenant": own["tenant"], "nro": nro})
        assert (stock, available, state) == (7, 6, "confirmada")
    finally:
        staff.close()
        unauthorized.close()
        with plataforma.begin() as connection:
            for tenant, reservation in reservation_ids:
                connection.execute(text("""
                    DELETE FROM bitacora WHERE idemp=:tenant
                      AND detalles->>'nroReserva'=:reservation
                """), {"tenant": tenant, "reservation": str(reservation)})


def test_cu22_vencimiento_respeta_tenant_y_libera_solo_su_sucursal(
        credenciales, reservas_clientes, catalogos_cu10, plataforma):
    own = catalogos_cu10["manaco"]
    foreign = catalogos_cu10["urbanwomen"]
    reservation_ids = []
    customers = []
    for code, account, catalog in (
            ("manaco", reservas_clientes["manaco"][0], own),
            ("urbanwomen", reservas_clientes["urbanwomen"][0], foreign)):
        customer = TestClient(create_app(_configuracion("cu10,cu11,cu22")),
                              raise_server_exceptions=False)
        customer.__enter__()
        customer.headers.update(CABECERAS)
        _login_owned(customer, account)
        created = customer.post("/api/cliente/reservas",
                                json=_reservation_payload(customer, catalog, 1))
        assert created.status_code == 201, created.text
        reservation_ids.append((catalog["tenant"], created.json()["nroReserva"]))
        customers.append(customer)
    staff = TestClient(create_app(_configuracion("cu10,cu11,cu22")),
                       raise_server_exceptions=False)
    staff.__enter__()
    staff.headers.update(CABECERAS)
    try:
        assert _entrar_empresa(staff, credenciales, "manaco", "encargado").status_code == 200
        manaco_nro = reservation_ids[0][1]
        with plataforma.begin() as connection:
            connection.execute(text("""
                UPDATE reserva SET fechareserva=CURRENT_DATE-1, horaatencion='00:00:00'
                WHERE idemp=:tenant AND nroreserva=:nro
            """), {"tenant": own["tenant"], "nro": manaco_nro})
        assert staff.get("/api/admin/reservas-sucursal").status_code == 200
        with plataforma.connect() as connection:
            own_state = connection.scalar(text(
                "SELECT estado FROM reserva WHERE idemp=:tenant AND nroreserva=:nro"
            ), {"tenant": own["tenant"], "nro": manaco_nro})
            foreign_state = connection.scalar(text(
                "SELECT estado FROM reserva WHERE idemp=:tenant AND nroreserva=:nro"
            ), {"tenant": foreign["tenant"], "nro": reservation_ids[1][1]})
        assert own_state == "vencida"
        assert foreign_state == "pendiente"
        assert _stock(plataforma, own["tenant"], own["branch"], own["variants"][0]) == 7
        assert _stock(plataforma, foreign["tenant"], foreign["branch"], foreign["variants"][0]) == 6
    finally:
        staff.close()
        for customer in customers:
            customer.close()
        with plataforma.begin() as connection:
            for tenant, reservation in reservation_ids:
                connection.execute(text("""
                    DELETE FROM bitacora WHERE idemp=:tenant
                      AND detalles->>'nroReserva'=:reservation
                """), {"tenant": tenant, "reservation": str(reservation)})


def _cu23_return_payload(sales, key, *, quantity=1, new_variant=None):
    tenant, sale_id = sales["sales"][key]
    account = sales["customers"]["manaco" if key != "extranjera" else "urbanwomen"]
    item = {"idDetalleVenta": 1, "cantidad": quantity}
    if new_variant is not None:
        item["idVarNueva"] = new_variant
    return {
        "idOperacion": str(uuid4()),
        "nroVenta": sale_id,
        "idCliente": account["user"],
        "motivo": "Prueba de integración CU23",
        "items": [item],
    }


def _cu23_enable_staff(staff, credenciales, role="encargado"):
    response = _entrar_empresa(staff, credenciales, "manaco", role)
    assert response.status_code == 200, response.json()


def _cu23_assert_inventory(platform, tenant, branch_id, variant, expected):
    with platform.connect() as connection:
        row = connection.execute(
            text("""
                SELECT stock, cantDisp, stockDanado FROM inventario
                WHERE idEmp=:tenant AND nroSuc=:branch AND idVar=:variant
            """),
            {"tenant": tenant, "branch": branch_id, "variant": variant},
        ).one()
    assert tuple(row) == expected


def test_cu23_devolucion_idempotente_inspeccion_danada_y_reembolso(
        cu23_clients, credenciales, cu23_sales, plataforma):
    staff, _ = cu23_clients
    _cu23_enable_staff(staff, credenciales)
    catalog = cu23_sales["catalogs"]["manaco"]
    payload = _cu23_return_payload(cu23_sales, "parcial")
    response = staff.post("/api/admin/devoluciones", json=payload)
    assert response.status_code == 201, response.text
    nro = response.json()["nroDev"]
    replay = staff.post("/api/admin/devoluciones", json=payload)
    assert replay.status_code == 201 and replay.json()["nroDev"] == nro
    with plataforma.connect() as connection:
        assert connection.scalar(
            text("SELECT count(*) FROM devolucion WHERE idEmp=:tenant AND nroDev=:nro"),
            {"tenant": catalog["tenant"], "nro": nro},
        ) == 1
        assert connection.scalar(
            text("SELECT count(*) FROM detalleDev WHERE idEmp=:tenant AND nroDev=:nro"),
            {"tenant": catalog["tenant"], "nro": nro},
        ) == 1

    inspected = staff.post(
        f"/api/admin/devoluciones/{nro}/decision",
        json={"accion": "inspeccionar",
              "inspecciones": [{"idDetalleVenta": 1, "condicion": "danada"}]},
    )
    assert inspected.status_code == 200, inspected.text
    assert inspected.json()["items"][0]["condicion"] == "danada"
    approved = staff.post(
        f"/api/admin/devoluciones/{nro}/decision",
        json={"accion": "aprobar"},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["estado"] == "aprobada"
    assert approved.json()["reembolso"]["estado"] == "pendiente"
    _cu23_assert_inventory(
        plataforma, catalog["tenant"], catalog["branch"], catalog["variants"][0],
        (4, 4, 1),
    )
    repeated = staff.post(
        f"/api/admin/devoluciones/{nro}/decision",
        json={"accion": "aprobar"},
    )
    assert repeated.json()["estado"] == "aprobada"
    with plataforma.connect() as connection:
        assert connection.scalar(
            text("SELECT count(*) FROM movimiento_inv WHERE idEmp=:tenant AND motivo=:reason"),
            {"tenant": catalog["tenant"], "reason": f"Devolución #{nro} (prenda dañada)"},
        ) == 1

    refunded = staff.post(f"/api/admin/devoluciones/{nro}/reembolsar")
    assert refunded.status_code == 200, refunded.text
    assert refunded.json()["reembolso"]["estado"] == "aprobado"
    second_payload = _cu23_return_payload(cu23_sales, "parcial")
    second_payload["items"][0]["cantidad"] = 1
    second = staff.post("/api/admin/devoluciones", json=second_payload)
    assert second.status_code == 201, second.text
    second_id = second.json()["nroDev"]
    accepted = staff.post(
        f"/api/admin/devoluciones/{second_id}/decision",
        json={"accion": "aprobar",
              "inspecciones": [{"idDetalleVenta": 1, "condicion": "apta"}]},
    )
    assert accepted.status_code == 200, accepted.text
    _cu23_assert_inventory(
        plataforma, catalog["tenant"], catalog["branch"], catalog["variants"][0],
        (5, 5, 1),
    )


def test_cu23_rechazo_no_modifica_inventario_y_es_repetible(
        cu23_clients, credenciales, cu23_sales, plataforma):
    staff, _ = cu23_clients
    _cu23_enable_staff(staff, credenciales)
    catalog = cu23_sales["catalogs"]["manaco"]
    response = staff.post(
        "/api/admin/devoluciones",
        json=_cu23_return_payload(cu23_sales, "parcial"),
    )
    assert response.status_code == 201, response.text
    nro = response.json()["nroDev"]

    rejected = staff.post(
        f"/api/admin/devoluciones/{nro}/decision",
        json={"accion": "rechazar"},
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["estado"] == "cancelada"
    repeated = staff.post(
        f"/api/admin/devoluciones/{nro}/decision",
        json={"accion": "rechazar"},
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["estado"] == "cancelada"
    with plataforma.connect() as connection:
        assert connection.scalar(
            text("SELECT estado FROM devolucion WHERE idEmp=:tenant AND nroDev=:nro"),
            {"tenant": catalog["tenant"], "nro": nro},
        ) == "rechazada"
    initial = cu23_sales["baseline"][
        (catalog["tenant"], catalog["branch"], catalog["variants"][0])
    ]
    _cu23_assert_inventory(
        plataforma, catalog["tenant"], catalog["branch"], catalog["variants"][0],
        (
            initial["stock"] - 3,
            initial["cantDisp"] - 3,
            initial["stockDanado"],
        ),
    )


def test_cu23_cambio_positivo_cobra_diferencia_sin_doble_stock(
        cu23_clients, credenciales, cu23_sales, reservas_clientes, plataforma):
    staff, customer = cu23_clients
    _cu23_enable_staff(staff, credenciales)
    catalog = cu23_sales["catalogs"]["manaco"]
    payload = _cu23_return_payload(
        cu23_sales, "positivo", new_variant=catalog["variants"][1]
    )
    response = staff.post("/api/admin/devoluciones/cambios", json=payload)
    assert response.status_code == 201, response.text
    created = response.json()
    nro = created["nroDev"]
    change = created["cambio"]
    assert Decimal(change["diferencia"]) == Decimal("111")
    assert change["estado"] == "pendiente"
    _cu23_assert_inventory(
        plataforma, catalog["tenant"], catalog["branch"], catalog["variants"][1],
        (6, 5, 0),
    )

    account = reservas_clientes["manaco"][0]
    _login_owned(customer, account)
    paid = customer.post(
        "/api/cliente/pagos",
        json={"nroVenta": change["nroVentaNueva"], "metodo": "tarjeta"},
    )
    assert paid.status_code == 201, paid.text
    assert paid.json()["estado"] == "aprobado"
    _cu23_assert_inventory(
        plataforma, catalog["tenant"], catalog["branch"], catalog["variants"][1],
        (6, 5, 0),
    )

    approved = staff.post(
        f"/api/admin/devoluciones/{nro}/decision",
        json={"accion": "aprobar",
              "inspecciones": [{"idDetalleVenta": 1, "condicion": "apta"}]},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["estado"] == "aprobada"
    assert approved.json()["reembolso"] is None
    assert approved.json()["cambio"]["estado"] == "aprobado"
    assert approved.json()["cambio"]["estadoPago"] == "aprobado"
    _cu23_assert_inventory(
        plataforma, catalog["tenant"], catalog["branch"], catalog["variants"][0],
        (5, 5, 0),
    )
    _cu23_assert_inventory(
        plataforma, catalog["tenant"], catalog["branch"], catalog["variants"][1],
        (5, 5, 0),
    )


def test_cu23_cambio_negativo_espera_confirmacion_de_reembolso(
        cu23_clients, credenciales, cu23_sales, plataforma):
    staff, _ = cu23_clients
    _cu23_enable_staff(staff, credenciales)
    catalog = cu23_sales["catalogs"]["manaco"]
    payload = _cu23_return_payload(
        cu23_sales, "negativo", new_variant=catalog["variants"][0]
    )
    created = staff.post("/api/admin/devoluciones/cambios", json=payload)
    assert created.status_code == 201, created.text
    nro = created.json()["nroDev"]
    change = created.json()["cambio"]
    assert Decimal(change["diferencia"]) == Decimal("-111")
    assert change["estado"] == "pendiente"
    assert change["nroVentaNueva"] is not None
    assert Decimal(created.json()["monto"]) == Decimal("222")

    approved = staff.post(
        f"/api/admin/devoluciones/{nro}/decision",
        json={"accion": "aprobar",
              "inspecciones": [{"idDetalleVenta": 1, "condicion": "apta"}]},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["estado"] == "aprobada"
    assert approved.json()["cambio"]["estado"] == "pendiente"
    assert approved.json()["reembolso"]["estado"] == "pendiente"
    assert Decimal(approved.json()["reembolso"]["monto"]) == Decimal("111")
    _cu23_assert_inventory(
        plataforma, catalog["tenant"], catalog["branch"], catalog["variants"][1],
        (6, 6, 0),
    )
    _cu23_assert_inventory(
        plataforma, catalog["tenant"], catalog["branch"], catalog["variants"][0],
        (4, 3, 0),
    )

    refunded = staff.post(f"/api/admin/devoluciones/{nro}/reembolsar")
    assert refunded.status_code == 200, refunded.text
    assert refunded.json()["reembolso"]["estado"] == "aprobado"
    assert refunded.json()["cambio"]["estado"] == "aprobado"
    _cu23_assert_inventory(
        plataforma, catalog["tenant"], catalog["branch"], catalog["variants"][1],
        (7, 7, 0),
    )
    _cu23_assert_inventory(
        plataforma, catalog["tenant"], catalog["branch"], catalog["variants"][0],
        (3, 3, 0),
    )


def test_cu23_limita_empresa_sucursal_permiso_y_concurrencia(
        cu23_clients, credenciales, cu23_sales, plataforma):
    staff, customer = cu23_clients
    _cu23_enable_staff(staff, credenciales)
    catalog = cu23_sales["catalogs"]["manaco"]
    foreign_tenant, foreign_sale = cu23_sales["sales"]["extranjera"]
    other_branch_tenant, other_branch_sale = cu23_sales["sales"]["otra_sucursal"]
    assert staff.get(f"/api/admin/devoluciones/ventas/{foreign_sale}").status_code == 404
    assert staff.get(f"/api/admin/devoluciones/ventas/{other_branch_sale}").status_code == 404

    payload = _cu23_return_payload(cu23_sales, "parcial", quantity=2)
    second_payload = dict(payload)
    second_payload["idOperacion"] = str(uuid4())

    def submit(request_payload):
        with TestClient(create_app(_configuracion("cu10,cu14,cu23")),
                        raise_server_exceptions=False) as client:
            client.headers.update(CABECERAS)
            _cu23_enable_staff(client, credenciales)
            return client.post("/api/admin/devoluciones", json=request_payload).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(submit, (payload, second_payload)))
    assert sorted(statuses) == [201, 409]
    with plataforma.connect() as connection:
        assert connection.scalar(
            text("""
                SELECT count(*) FROM devolucion
                WHERE idEmp=:tenant AND nroVenta=:sale AND estado='pendiente'
            """),
            {"tenant": catalog["tenant"], "sale": cu23_sales["sales"]["parcial"][1]},
        ) == 1

    assert _entrar_empresa(customer, credenciales, "manaco", "cajero").status_code == 200
    assert customer.get("/api/admin/devoluciones").status_code == 403
    customer.cookies.clear()
    assert customer.get("/api/admin/devoluciones").status_code == 401
    assert foreign_tenant != catalog["tenant"]
    assert other_branch_tenant == catalog["tenant"]


def test_cu23_fallo_de_trigger_revierte_aprobacion_y_existencias(
        cu23_clients, credenciales, cu23_sales, reservas_clientes, plataforma):
    staff, customer = cu23_clients
    _cu23_enable_staff(staff, credenciales)
    catalog = cu23_sales["catalogs"]["manaco"]
    payload = _cu23_return_payload(
        cu23_sales, "positivo", new_variant=catalog["variants"][1]
    )
    created = staff.post("/api/admin/devoluciones/cambios", json=payload)
    assert created.status_code == 201, created.text
    nro = created.json()["nroDev"]
    change = created.json()["cambio"]
    _login_owned(customer, reservas_clientes["manaco"][0])
    paid = customer.post(
        "/api/cliente/pagos",
        json={"nroVenta": change["nroVentaNueva"], "metodo": "tarjeta"},
    )
    assert paid.status_code == 201, paid.text

    with plataforma.begin() as connection:
        connection.execute(
            text("SELECT set_config('app.idemp', :tenant, true)"),
            {"tenant": str(catalog["tenant"])},
        )
        connection.execute(
            text("""
                UPDATE inventario SET stock=0, cantDisp=0
                WHERE idEmp=:tenant AND nroSuc=:branch AND idVar=:variant
            """),
            {
                "tenant": catalog["tenant"],
                "branch": catalog["branch"],
                "variant": catalog["variants"][1],
            },
        )
    failed = staff.post(
        f"/api/admin/devoluciones/{nro}/decision",
        json={"accion": "aprobar",
              "inspecciones": [{"idDetalleVenta": 1, "condicion": "apta"}]},
    )
    assert failed.status_code == 503
    with plataforma.connect() as connection:
        state = connection.execute(
            text("""
                SELECT d.estado, c.estado
                FROM devolucion d
                JOIN cambio c ON c.idEmp=d.idEmp AND c.nroDev=d.nroDev
                WHERE d.idEmp=:tenant AND d.nroDev=:nro
            """),
            {"tenant": catalog["tenant"], "nro": nro},
        ).one()
        refund_count = connection.scalar(
            text("SELECT count(*) FROM reembolso WHERE idEmp=:tenant AND nroDev=:nro"),
            {"tenant": catalog["tenant"], "nro": nro},
        )
        movements = connection.scalar(
            text("""
                SELECT count(*) FROM movimiento_inv
                WHERE idEmp=:tenant AND motivo LIKE :reason
            """),
            {"tenant": catalog["tenant"], "reason": f"Cambio #{change['nroCambio']}%"},
        )
    assert tuple(state) == ("pendiente", "pendiente")
    assert refund_count == 0 and movements == 0
    _cu23_assert_inventory(
        plataforma, catalog["tenant"], catalog["branch"], catalog["variants"][0],
        (4, 4, 0),
    )


@pytest.fixture
def autonomo_report_admins(plataforma, credenciales, catalogos_cu10):
    accounts = {}
    with plataforma.begin() as conn:
        for code, catalog in catalogos_cu10.items():
            original = next(e for e in credenciales['empresas'] if e['codigo']==code)['usuarios']['admin']
            accounts[code] = []
            for _ in range(2):
                user = 'AR'+uuid4().hex
                email = user.lower()+'@example.com'
                conn.execute(text("SELECT set_config('app.idemp', :tenant, true)"),{'tenant':str(catalog['tenant'])})
                conn.execute(text("""INSERT INTO usuario(idusuario,ci,nombre,apellidopat,apellidomat,sexo,correo,telefono,
                    direccion,contrasena,fechanac,tipo,nrorol,idemp)
                    SELECT :user,:ci,'Reporte prueba','Propio','Temporal',sexo,:email,telefono,direccion,
                      contrasena,fechanac,'A',nrorol,idemp FROM usuario
                    WHERE idemp=:tenant AND lower(btrim(correo))=:original"""),
                    {'user':user,'ci':user[:16],'email':email,'tenant':catalog['tenant'],'original':original['correo'].lower()})
                accounts[code].append(dict(user=user,email=email,password=original['contrasena'],code=code,tenant=catalog['tenant']))
    try:
        yield accounts
    finally:
        with plataforma.begin() as conn:
            for rows in accounts.values():
                for row in rows:
                    conn.execute(text("SELECT set_config('app.idemp', :tenant, true)"),{'tenant':str(row['tenant'])})
                    conn.execute(text('DELETE FROM usuario WHERE idemp=:tenant AND idusuario=:user'),row)


@pytest.fixture
def autonomo_report_sales(plataforma, catalogos_cu10):
    owned=[]
    def create(catalog):
        with plataforma.begin() as conn:
            params={'tenant':catalog['tenant'],'branch':catalog['branch'],'variant':catalog['variants'][0]}
            conn.execute(text("SELECT set_config('app.idemp', :tenant, true)"),{'tenant':str(params['tenant'])})
            conn.execute(text('LOCK TABLE venta,pago IN SHARE ROW EXCLUSIVE MODE'))
            params['sale']=conn.scalar(text('SELECT coalesce(max(nroventa),0)+1 FROM venta'))
            params['payment']=conn.scalar(text('SELECT coalesce(max(idpago),0)+1 FROM pago'))
            conn.execute(text("INSERT INTO venta(nroventa,fechahora,total,estado,nrosuc,idemp) VALUES (:sale,CURRENT_TIMESTAMP,222,'registrada',:branch,:tenant)"),params)
            conn.execute(text('INSERT INTO detalleventa(nroventa,iddetalleventa,cantidad,preciounitario,idvar,idemp) VALUES (:sale,1,2,111,:variant,:tenant)'),params)
            conn.execute(text("INSERT INTO pago(idpago,metodo,monto,estado,nroventa,idemp) VALUES (:payment,'efectivo',222,'aprobado',:sale,:tenant)"),params)
            conn.execute(text('UPDATE inventario SET stock=stock-2,cantdisp=cantdisp-2 WHERE idemp=:tenant AND nrosuc=:branch AND idvar=:variant'),params)
            owned.append(params)
        return params['sale']
    try:
        yield create
    finally:
        with plataforma.begin() as conn:
            for params in owned:
                conn.execute(text("SELECT set_config('app.idemp', :tenant, true)"),{'tenant':str(params['tenant'])})
                conn.execute(text('DELETE FROM pago WHERE idemp=:tenant AND idpago=:payment'),params)
                conn.execute(text('DELETE FROM detalleventa WHERE idemp=:tenant AND nroventa=:sale'),params)
                conn.execute(text('DELETE FROM venta WHERE idemp=:tenant AND nroventa=:sale'),params)
                conn.execute(text('UPDATE inventario SET stock=stock+2,cantdisp=cantdisp+2 WHERE idemp=:tenant AND nrosuc=:branch AND idvar=:variant'),params)


@pytest.fixture
def autonomo_inventory_movements(plataforma,catalogos_cu10):
    yield
    with plataforma.begin() as conn:
        for catalog in catalogos_cu10.values():
            conn.execute(text('DELETE FROM movimiento_inv WHERE idemp=:tenant AND nroinv IN '
                              '(SELECT nroinv FROM inventario WHERE idemp=:tenant AND idvar=ANY(:variants))'),
                         {'tenant':catalog['tenant'],'variants':catalog['variants']})


@pytest.mark.parametrize('code,other',[('manaco','urbanwomen'),('urbanwomen','manaco')])
def test_autonomo_inventario_http_concurrencia_y_referencias(
        plataforma,catalogos_cu10,autonomo_report_admins,autonomo_inventory_movements,code,other):
    catalog=catalogos_cu10[code];foreign=catalogos_cu10[other]
    accounts=autonomo_report_admins[code]
    base='/api/admin/inventario'
    with TestClient(create_app(_configuracion('cu20')),raise_server_exceptions=False) as client:
        client.headers.update(CABECERAS)
        _login_report_user(client,accounts[0])
        listed=client.get(base,params={'q':catalog['marker']})
        assert listed.status_code==200
        assert {r['idVariante'] for r in listed.json()['items']}.issubset(set(catalog['variants']))
        payload={'nroSuc':catalog['branch'],'idVariante':catalog['variants'][0],'tipoMov':'entrada','cantidad':3,'motivo':'Fixture autónomo inventario'}
        added=client.post(base+'/movimientos',json=payload)
        assert added.status_code==201
        assert (added.json()['stock'],added.json()['cantDisp'])==(10,10)
        inv=added.json()['nroInv']
        assert client.get(f'{base}/{inv}/movimientos').json()['total']==1
        assert client.post(base+'/movimientos',json=payload|{'idVariante':foreign['variants'][0]}).status_code==422
        assert client.post(base+'/movimientos',json=payload|{'nroSuc':foreign['branch']}).status_code==422
        assert client.post(base+'/movimientos',json=payload|{'nroSuc':catalog['closed']}).status_code==409
        assert client.get(base,params={'idemp':foreign['tenant']}).status_code==422
        with plataforma.begin() as conn:
            conn.execute(text('UPDATE inventario SET cantdisp=8 WHERE idemp=:tenant AND nroinv=:id'),{'tenant':catalog['tenant'],'id':inv})
        assert client.post(base+'/movimientos',json=payload|{'tipoMov':'salida','cantidad':9}).status_code==409
        def withdraw(account):
            with TestClient(create_app(_configuracion('cu20')),raise_server_exceptions=False) as c:
                c.headers.update(CABECERAS);_login_report_user(c,account)
                return c.post(base+'/movimientos',json=payload|{'tipoMov':'salida','cantidad':6}).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses=list(pool.map(withdraw,accounts))
        assert sorted(statuses)==[201,409]
        _login_report_user(client,accounts[0])
        assert client.get(f'{base}/{inv}/movimientos').json()['total']==2
        with plataforma.connect() as conn:
            final=conn.execute(text('SELECT stock,cantdisp FROM inventario WHERE idemp=:tenant AND nroinv=:id'),{'tenant':catalog['tenant'],'id':inv}).one()
            assert tuple(final)==(4,2)
        _login_report_user(client,autonomo_report_admins[other][0])
        assert client.get(f'{base}/{inv}/movimientos').status_code==404


@pytest.fixture
def autonomo_suppliers(plataforma,catalogos_cu10):
    owned=[]
    yield owned
    with plataforma.begin() as conn:
        for tenant,supplier in owned:
            conn.execute(text('DELETE FROM proveedor WHERE idemp=:tenant AND idprov=:supplier AND nombre LIKE :marker'),
                         {'tenant':tenant,'supplier':supplier,'marker':'AUTONOMO-PROV-%'})


@pytest.mark.parametrize('code,other',[('manaco','urbanwomen'),('urbanwomen','manaco')])
def test_autonomo_proveedores_crud_tenant_y_referencias(
        catalogos_cu10,autonomo_report_admins,autonomo_suppliers,code,other):
    own=catalogos_cu10[code];foreign=catalogos_cu10[other]
    with TestClient(create_app(_configuracion('cu19')),raise_server_exceptions=False) as client:
        client.headers.update(CABECERAS);base='/api/admin/proveedores'
        _login_report_user(client,autonomo_report_admins[code][0])
        marker='AUTONOMO-PROV-'+uuid4().hex[:12]
        payload={'nombre':marker,'correo':marker.lower()+'@example.com','direccion':'Dirección de prueba','telefono':'70000000'}
        created=client.post(base,json=payload)
        assert created.status_code==201
        supplier=created.json()['id'];autonomo_suppliers.append((own['tenant'],supplier))
        assert client.get(base,params={'q':marker}).json()['total']==1
        assert client.get(f'{base}/{supplier}').json()['nombre']==marker
        _login_report_user(client,autonomo_report_admins[other][0])
        assert client.get(f'{base}/{supplier}').status_code==404
        assert client.put(f'{base}/{supplier}',json=payload).status_code==404
        assert client.delete(f'{base}/{supplier}').status_code==404
        assert client.get(base,params={'q':marker}).json()['total']==0
        _login_report_user(client,autonomo_report_admins[code][1])
        assert client.put(f'{base}/{supplier}',json=payload|{'direccion':'Dirección actualizada'}).status_code==200
        assert client.post(base,json=payload|{'idemp':foreign['tenant']}).status_code==422
        assert client.get(base,params={'tenant':foreign['tenant']}).status_code==422
        assert client.delete(f'{base}/{supplier}').status_code==204
        assert client.get(f'{base}/{supplier}').status_code==404
        # Existing suppliers referenced by the company's own products cannot cascade-delete them.
        assert client.delete(f"{base}/{own['provider']}").status_code==409


@pytest.fixture
def autonomo_products(plataforma,catalogos_cu10):
    owned=[]
    yield owned
    with plataforma.begin() as conn:
        for tenant,product in owned:
            params={'tenant':tenant,'product':product}
            # Every row is reached through this test's exact product ID.
            for statement in (
                'DELETE FROM movimiento_inv WHERE idemp=:tenant AND nroinv IN (SELECT i.nroinv FROM inventario i JOIN varianteprod v ON v.idemp=i.idemp AND v.idvariante=i.idvar WHERE v.idemp=:tenant AND v.idprod=:product)',
                'DELETE FROM inventario WHERE idemp=:tenant AND idvar IN (SELECT idvariante FROM varianteprod WHERE idemp=:tenant AND idprod=:product)',
                'DELETE FROM variantecolors WHERE idemp=:tenant AND idvar IN (SELECT idvariante FROM varianteprod WHERE idemp=:tenant AND idprod=:product)',
                'DELETE FROM varianteprod WHERE idemp=:tenant AND idprod=:product',
                'DELETE FROM producto WHERE idemp=:tenant AND idprod=:product',
            ): conn.execute(text(statement),params)


@pytest.mark.parametrize('code,other',[('manaco','urbanwomen'),('urbanwomen','manaco')])
def test_autonomo_catalogo_productos_variantes_y_referencias(
        plataforma,catalogos_cu10,autonomo_report_admins,autonomo_products,code,other):
    own=catalogos_cu10[code];foreign=catalogos_cu10[other];base='/api/admin/catalogo'
    with TestClient(create_app(_configuracion('cu18,cu20')),raise_server_exceptions=False) as client:
        client.headers.update(CABECERAS);_login_report_user(client,autonomo_report_admins[code][0])
        marker='APROD-'+uuid4().hex[:16]
        variant={'sku':marker,'precio':'25.50','estado':'activo','idTalla':own['talla'],'idColores':[own['color']]}
        payload={'descripcion':marker,'estado':'activo','idCat':own['cat'],'idMarca':own['brand'],
                 'idCol':own['col'],'idProv':own['provider'],'idPromo':None,'variantes':[variant]}
        for key,value in [('idCat',foreign['cat']),('idMarca',foreign['brand']),('idCol',foreign['col']),('idProv',foreign['provider'])]:
            assert client.post(base+'/productos',json=payload|{key:value}).status_code==422
        assert client.post(base+'/productos',json=payload|{'variantes':[variant|{'idTalla':foreign['talla']}]}).status_code==422
        assert client.post(base+'/productos',json=payload|{'variantes':[variant|{'idColores':[foreign['color']]}]}).status_code==422
        created=client.post(base+'/productos',json=payload)
        assert created.status_code==201
        product=created.json()['idProd'];autonomo_products.append((own['tenant'],product))
        var=created.json()['variantes'][0]['idVariante']
        assert created.json()['variantes'][0]['idColores']==[own['color']]
        assert client.get(base+'/productos',params={'q':marker}).json()['total']==1
        detail=client.get(f'{base}/productos/{product}')
        assert detail.status_code==200 and Decimal(detail.json()['variantes'][0]['precio'])==Decimal('25.50')
        _login_report_user(client,autonomo_report_admins[other][0])
        assert client.get(f'{base}/productos/{product}').status_code==404
        assert client.put(f'{base}/productos/{product}',json=payload).status_code==404
        assert client.delete(f'{base}/productos/{product}').status_code==404
        _login_report_user(client,autonomo_report_admins[code][1])
        updated=payload|{'descripcion':marker+' editado','variantes':[variant|{'idVariante':var,'precio':'30.00'}]}
        assert client.put(f'{base}/productos/{product}',json=updated).status_code==200
        assert client.put(f'{base}/productos/{product}',json=payload|{'variantes':[variant|{'idVariante':own['variants'][0]}]}).status_code==422
        # A product with inventory/history is not silently cascade-deleted.
        movement={'nroSuc':own['branch'],'idVariante':var,'tipoMov':'entrada','cantidad':3,'motivo':'Fixture producto autónomo'}
        assert client.post('/api/admin/inventario/movimientos',json=movement).status_code==201
        assert client.delete(f'{base}/productos/{product}').status_code==409
        assert client.put(f'{base}/productos/{product}',json=payload).status_code==409
        final=client.get(f'{base}/productos/{product}').json()
        assert final['variantes'][0]['idVariante']==var and Decimal(final['variantes'][0]['precio'])==Decimal('30')
        assert client.post(base+'/productos',json=payload|{'idEmp':foreign['tenant']}).status_code==422
        disposable=client.post(base+'/productos',json=payload|{'variantes':[variant|{'sku':marker+'B'}]})
        assert disposable.status_code==201
        removable=disposable.json()['idProd'];autonomo_products.append((own['tenant'],removable))
        assert client.delete(f'{base}/productos/{removable}').status_code==204
        assert client.get(f'{base}/productos/{removable}').status_code==404
        assert client.post(base+'/categorias',json={'descripcion':'No permitido','idEmp':foreign['tenant']}).status_code==422
        assert client.post(base+'/imagenes').status_code in (422, 503)


def test_autonomo_encargado_inventario_solo_sucursal_asignada(
        plataforma,catalogos_cu10,autonomo_report_admins,autonomo_inventory_movements):
    catalog=catalogos_cu10['manaco'];account=autonomo_report_admins['manaco'][1]
    params={'tenant':catalog['tenant'],'user':account['user'],'branch':catalog['branch'],'code':uuid4().hex[:10]}
    with plataforma.begin() as conn:
        conn.execute(text('UPDATE usuario SET tipo=\'E\' WHERE idemp=:tenant AND idusuario=:user'),params)
        conn.execute(text("INSERT INTO empleado(idusuario,cod_emp,cargo,nrosuc,estado,idemp) VALUES (:user,:code,'Encargada',:branch,'activo',:tenant)"),params)
    try:
        with TestClient(create_app(_configuracion('cu20')),raise_server_exceptions=False) as client:
            client.headers.update(CABECERAS);_login_report_user(client,account);base='/api/admin/inventario'
            refs=client.get(base+'/referencias')
            assert refs.status_code==200
            assert [row['id'] for row in refs.json()['sucursales']]==[catalog['branch']]
            assert refs.json()['sucursalAsignada']==catalog['branch']
            listing=client.get(base,params={'q':catalog['marker']})
            assert listing.status_code==200 and all(row['nroSuc']==catalog['branch'] for row in listing.json()['items'])
            assert client.get(base,params={'nroSuc':catalog['closed']}).status_code==403
            payload={'nroSuc':catalog['branch'],'idVariante':catalog['variants'][0],'tipoMov':'entrada','cantidad':1,'motivo':'Fixture encargado propio'}
            assert client.post(base+'/movimientos',json=payload).status_code==201
            assert client.post(base+'/movimientos',json=payload|{'nroSuc':catalog['closed']}).status_code==403
            with plataforma.begin() as conn:
                conn.execute(text("UPDATE empleado SET estado='inactivo' WHERE idemp=:tenant AND idusuario=:user"),params)
            # current_identity rejects an inactive employee before permission evaluation.
            assert client.post(base+'/movimientos',json=payload).status_code==401
    finally:
        with plataforma.begin() as conn:
            conn.execute(text('DELETE FROM empleado WHERE idemp=:tenant AND idusuario=:user'),params)


@pytest.fixture
def autonomo_references(plataforma,catalogos_cu10):
    owned=[]
    yield owned
    with plataforma.begin() as conn:
        for table,key,tenant,row_id in reversed(owned):
            if table=='coleccion':
                conn.execute(text('DELETE FROM tempcoleccion WHERE idemp=:tenant AND idcol=:id'),{'tenant':tenant,'id':row_id})
            conn.execute(text(f'DELETE FROM {table} WHERE idemp=:tenant AND {key}=:id'),{'tenant':tenant,'id':row_id})


@pytest.mark.parametrize('code,other',[('manaco','urbanwomen'),('urbanwomen','manaco')])
def test_autonomo_catalogo_referencias_crud_y_colecciones(
        catalogos_cu10,autonomo_report_admins,autonomo_references,code,other):
    own=catalogos_cu10[code];foreign=catalogos_cu10[other];base='/api/admin/catalogo'
    marker='AREF'+uuid4().hex[:8]
    groups=[('tallas','talla','idtalla',{'descripcion':marker[:8]}),
            ('colores','color','idcolor',{'descripcion':marker,'hex':'#123456'}),
            ('categorias','categoria','idcat',{'descripcion':marker}),
            ('marcas','marca','idmarca',{'nombre':marker,'estado':'activo'}),
            ('temporadas','temporada','idtemp',{'nombre':marker,'fechaIni':'2026-01-01','fechaFin':'2026-12-31','estado':'activo'}),
            ('colecciones','coleccion','idcol',{'descripcion':marker,'idTemps':[own['season']]})]
    with TestClient(create_app(_configuracion('cu18')),raise_server_exceptions=False) as client:
        client.headers.update(CABECERAS)
        for group,table,key,payload in groups:
            _login_report_user(client,autonomo_report_admins[code][0])
            created=client.post(f'{base}/{group}',json=payload)
            assert created.status_code==201
            row=created.json()['id'];autonomo_references.append((table,key,own['tenant'],row))
            assert client.get(f'{base}/{group}/{row}').status_code==200
            assert client.put(f'{base}/{group}/{row}',json=payload).status_code==200
            _login_report_user(client,autonomo_report_admins[other][0])
            assert client.get(f'{base}/{group}/{row}').status_code==404
            assert client.put(f'{base}/{group}/{row}',json=payload).status_code==404
            assert client.delete(f'{base}/{group}/{row}').status_code==404
            _login_report_user(client,autonomo_report_admins[code][1])
            assert client.delete(f'{base}/{group}/{row}').status_code==204
            assert client.get(f'{base}/{group}/{row}').status_code==404
        assert client.post(base+'/colecciones',json={'descripcion':marker,'idTemps':[foreign['season']]}).status_code==422


@pytest.fixture
def autonomo_promotions(plataforma,catalogos_cu10):
    original={}
    with plataforma.connect() as conn:
        for code,catalog in catalogos_cu10.items():
            original[code]=conn.scalar(text('SELECT idpromo FROM producto WHERE idemp=:tenant AND idprod=:id'),{'tenant':catalog['tenant'],'id':catalog['products'][0]})
    owned=[]
    yield owned
    with plataforma.begin() as conn:
        for code,catalog in catalogos_cu10.items():
            conn.execute(text('UPDATE producto SET idpromo=:promo WHERE idemp=:tenant AND idprod=:id'),
                         {'promo':original[code],'tenant':catalog['tenant'],'id':catalog['products'][0]})
        for tenant,promo in owned:
            conn.execute(text('DELETE FROM promocion WHERE idemp=:tenant AND idpromo=:promo'),{'tenant':tenant,'promo':promo})


@pytest.mark.parametrize('code,other',[('manaco','urbanwomen'),('urbanwomen','manaco')])
def test_autonomo_promociones_concurrencia_tenant_y_cancelacion(
        plataforma,catalogos_cu10,autonomo_report_admins,autonomo_promotions,code,other):
    from datetime import timedelta
    own=catalogos_cu10[code];foreign=catalogos_cu10[other];base='/api/admin/promociones'
    marker='APROMO'+uuid4().hex[:12]
    payload={'nombre':marker,'descripcion':'Promoción propia de prueba','tipoDescuento':'porcentaje','valorDescuento':'10',
             'fechaIni':date.today().isoformat(),'fechaFin':(date.today()+timedelta(days=5)).isoformat(),'productos':[own['products'][0]]}
    with TestClient(create_app(_configuracion('cu21')),raise_server_exceptions=False) as client:
        client.headers.update(CABECERAS);_login_report_user(client,autonomo_report_admins[code][0])
        assert client.post(base,json=payload|{'productos':[foreign['products'][0]]}).status_code==404
        assert client.post(base,json=payload|{'productos':[own['products'][0],foreign['products'][0]]}).status_code==404
        def create(account):
            with TestClient(create_app(_configuracion('cu21')),raise_server_exceptions=False) as c:
                c.headers.update(CABECERAS);_login_report_user(c,account)
                response=c.post(base,json=payload)
                return response.status_code,response.json()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(create,autonomo_report_admins[code]))
        assert sorted(status for status,_ in results)==[201,409]
        data=next(value for status,value in results if status==201);promo=data['idPromo'];autonomo_promotions.append((own['tenant'],promo))
        listed=client.get(base,params={'q':marker})
        assert listed.status_code==200 and listed.json()['total']==1
        assert listed.json()['items'][0]['productos']==[own['products'][0]]
        _login_report_user(client,autonomo_report_admins[other][0])
        assert client.get(base,params={'q':marker}).json()['total']==0
        assert client.put(f'{base}/{promo}',json=payload).status_code==404
        assert client.post(f'{base}/{promo}/desactivar').status_code==404
        _login_report_user(client,autonomo_report_admins[code][1])
        assert client.put(f'{base}/{promo}',json=payload|{'valorDescuento':'15'}).status_code==200
        for _ in range(2):assert client.post(f'{base}/{promo}/desactivar').json()['estado']=='cancelada'
        assert client.put(f'{base}/{promo}',json=payload).status_code==409


@pytest.fixture
def autonomo_checkout_client():
    with TestClient(create_app(_configuracion('cu10,cu12,cu13,cu14,cu15')),raise_server_exceptions=False) as client:
        client.headers.update(CABECERAS);yield client


@pytest.mark.parametrize('code,other',[('manaco','urbanwomen'),('urbanwomen','manaco')])
def test_autonomo_checkout_pago_mock_tenant_propiedad_timeout_y_reintento(
        autonomo_checkout_client,reservas_clientes,catalogos_cu10,plataforma,code,other):
    client=autonomo_checkout_client;own=catalogos_cu10[code];foreign=catalogos_cu10[other]
    a,b=reservas_clientes[code];_login_owned(client,a)
    assert client.post('/api/cliente/carrito/items',json={'idVar':own['variants'][0],'cantidad':2}).status_code==201
    body={'nroSuc':own['branch']}
    assert client.post('/api/cliente/compras/desde-carrito',json=body|{'nroSuc':foreign['branch']}).status_code==404
    created=client.post('/api/cliente/compras/desde-carrito',json=body)
    assert created.status_code==201
    sale=created.json()['nroVenta'];assert Decimal(created.json()['total'])==Decimal('222')
    assert _stock(plataforma,own['tenant'],own['branch'],own['variants'][0])==7
    repeated=client.post('/api/cliente/compras/desde-carrito',json=body)
    assert repeated.status_code==200 and repeated.json()['nroVenta']==sale
    _login_owned(client,b)
    assert client.get(f'/api/cliente/compras/{sale}').status_code==404
    assert client.get('/api/cliente/historial-compras').json()['total']==0
    assert client.get(f'/api/cliente/historial-compras/{sale}').status_code==404
    payment={'nroVenta':sale,'metodo':'tarjeta','escenario':'timeout'}
    assert client.post('/api/cliente/pagos',json=payment).status_code==404
    _login_owned(client,reservas_clientes[other][0])
    assert client.get(f'/api/cliente/compras/{sale}').status_code==404
    assert client.get(f'/api/cliente/historial-compras/{sale}').status_code==404
    _login_owned(client,a)
    config=client.get('/api/cliente/pagos/configuracion').json()
    assert config['proveedor']=='mock' and config['simulacion'] is True
    paid=client.post('/api/cliente/pagos',json=payment)
    assert paid.status_code==201 and paid.json()['estado']=='pendiente'
    pid=paid.json()['idPago']
    assert client.post('/api/cliente/pagos',json=payment|{'escenario':'aprobado'}).status_code==200
    assert _stock(plataforma,own['tenant'],own['branch'],own['variants'][0])==7
    _login_owned(client,b)
    assert client.get(f'/api/cliente/pagos/{pid}').status_code==404
    assert client.post(f'/api/cliente/pagos/{pid}/procesar',json={'escenario':'aprobado'}).status_code==404
    assert client.post(f'/api/cliente/compras/{sale}/cancelar').status_code==404
    _login_owned(client,a)
    for _ in range(2):
        approved=client.post(f'/api/cliente/pagos/{pid}/procesar',json={'escenario':'aprobado'})
        assert approved.status_code==200 and approved.json()['estado']=='aprobado'
        assert approved.json()['referencia'].startswith('MOCK-')
        assert _stock(plataforma,own['tenant'],own['branch'],own['variants'][0])==5
    assert client.post(f'/api/cliente/compras/{sale}/cancelar').status_code==409
    with plataforma.connect() as conn:
        params={'tenant':own['tenant'],'sale':sale,'motive':f'venta digital nro {sale}'}
        assert conn.scalar(text("SELECT count(*) FROM pago WHERE idemp=:tenant AND nroventa=:sale AND estado='aprobado'"),params)==1
        assert conn.scalar(text('SELECT count(*) FROM movimiento_inv WHERE idemp=:tenant AND motivo=:motive'),params)==1
    assert client.get('/api/cliente/carrito').json()['idCarrito'] is None
    history=client.get('/api/cliente/historial-compras?limit=1')
    assert history.status_code==200 and history.json()['total']==1
    assert history.json()['items'][0]['estadoPago']=='aprobado'
    with plataforma.begin() as conn:
        conn.execute(text('UPDATE varianteprod SET precio=999 WHERE idemp=:tenant AND idvariante=:var'),{'tenant':own['tenant'],'var':own['variants'][0]})
    frozen=client.get(f'/api/cliente/historial-compras/{sale}')
    assert frozen.status_code==200 and Decimal(frozen.json()['total'])==Decimal('222')
    assert Decimal(frozen.json()['items'][0]['precioUnitario'])==Decimal('111')
    assert client.post('/api/cliente/pagos',json=payment|{'idemp':foreign['tenant']}).status_code==422
    assert client.post('/api/integraciones/stripe/webhook').status_code==503


def test_autonomo_checkout_concurrente_ultima_unidad_solo_un_pago_aprobado(
        autonomo_checkout_client,reservas_clientes,catalogos_cu10,plataforma):
    own=catalogos_cu10['manaco'];accounts=reservas_clientes['manaco'];sales={}
    with plataforma.begin() as conn:
        conn.execute(text('UPDATE inventario SET stock=1,cantdisp=1 WHERE idemp=:tenant AND nrosuc=:branch AND idvar=:var'),
                     {'tenant':own['tenant'],'branch':own['branch'],'var':own['variants'][0]})
    for account in accounts:
        _login_owned(autonomo_checkout_client,account)
        assert autonomo_checkout_client.post('/api/cliente/carrito/items',json={'idVar':own['variants'][0],'cantidad':1}).status_code==201
        prepared=autonomo_checkout_client.post('/api/cliente/compras/desde-carrito',json={'nroSuc':own['branch']})
        assert prepared.status_code==201
        sales[account['user']]=prepared.json()['nroVenta']
    def pay(account):
        with TestClient(create_app(_configuracion('cu14')),raise_server_exceptions=False) as client:
            client.headers.update(CABECERAS);_login_owned(client,account)
            response=client.post('/api/cliente/pagos',json={'nroVenta':sales[account['user']],'metodo':'tarjeta','escenario':'aprobado'})
            return response.status_code,response.json()
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(pay,accounts))
    assert all(status==201 for status,_ in results)
    assert sorted(result['estado'] for _,result in results)==['aprobado','rechazado']
    assert _stock(plataforma,own['tenant'],own['branch'],own['variants'][0])==0
    with plataforma.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM pago WHERE idemp=:tenant AND nroventa=ANY(:sales) AND estado='aprobado'"),
                           {'tenant':own['tenant'],'sales':list(sales.values())})==1


def test_autonomo_checkout_cancelacion_y_proveedor_no_verificado(
        autonomo_checkout_client,reservas_clientes,catalogos_cu10,plataforma):
    client=autonomo_checkout_client;own=catalogos_cu10['manaco'];_login_owned(client,reservas_clientes['manaco'][0])
    assert client.post('/api/cliente/carrito/items',json={'idVar':own['variants'][0],'cantidad':1}).status_code==201
    first=client.post('/api/cliente/compras/desde-carrito',json={'nroSuc':own['branch']})
    assert first.status_code==201
    sale=first.json()['nroVenta']
    rejected=client.post('/api/cliente/pagos',json={'nroVenta':sale,'metodo':'QR','escenario':'rechazado'})
    assert rejected.status_code==201 and rejected.json()['estado']=='rechazado'
    second=client.post('/api/cliente/compras/desde-carrito',json={'nroSuc':own['branch']})
    assert second.status_code==201 and second.json()['nroVenta']!=sale
    sale=second.json()['nroVenta']
    pending=client.post('/api/cliente/pagos',json={'nroVenta':sale,'metodo':'QR','escenario':'timeout'})
    assert pending.status_code==201 and pending.json()['estado']=='pendiente'
    for _ in range(2):assert client.post(f'/api/cliente/compras/{sale}/cancelar').json()['estado']=='anulada'
    assert client.get(f"/api/cliente/pagos/{pending.json()['idPago']}").json()['estado']=='rechazado'
    assert _stock(plataforma,own['tenant'],own['branch'],own['variants'][0])==7
    client.app.state.settings.payments_provider='stripe'
    assert client.post('/api/cliente/compras/desde-carrito',json={'nroSuc':own['branch']}).status_code==503
    assert client.post('/api/cliente/pagos',json={'nroVenta':sale,'metodo':'tarjeta'}).status_code==503


def _login_report_user(client, account):
    client.cookies.clear()
    response = client.post('/api/auth/login/admin',json={'empresa':account['code'],'correo':account['email'],'contrasena':account['password']})
    assert response.status_code == 200


@pytest.mark.parametrize('code,other',[('manaco','urbanwomen'),('urbanwomen','manaco')])
def test_autonomo_reportes_reales_exportacion_y_propiedad(
        plataforma, catalogos_cu10, autonomo_report_admins, autonomo_report_sales, reservas_clientes, code, other):
    from io import BytesIO
    from zipfile import ZipFile
    catalog = catalogos_cu10[code]
    foreign = catalogos_cu10[other]
    with TestClient(create_app(_configuracion('cu25,sync')),raise_server_exceptions=False) as client:
        client.headers.update(CABECERAS)
        _login_report_user(client,autonomo_report_admins[code][0])
        base='/api/admin/reportes'
        refs=client.get(base+'/dinamicos/referencias')
        assert refs.status_code==200
        assert foreign['branch'] not in {r['nro'] for r in refs.json()['sucursales']}
        plan={'tipo':'ventas_sucursal','fechaIni':date.today().isoformat(),'fechaFin':date.today().isoformat(),
              'nroSuc':catalog['branch'],'limite':100}
        before=client.post(base+'/dinamicos/ejecutar',json=plan)
        assert before.status_code==200
        total_before=sum(Decimal(row['importe']) for row in before.json()['filas'])
        # Own paid cash-sale fixture, not an electronic payment approval path.
        autonomo_report_sales(catalog)
        after=client.post(base+'/dinamicos/ejecutar',json=plan)
        assert after.status_code==200
        assert sum(Decimal(row['importe']) for row in after.json()['filas'])==total_before+Decimal('222')
        top=client.post(base+'/dinamicos/ejecutar',json=plan|{'tipo':'productos_vendidos'})
        assert top.status_code==200
        own_product=next(row for row in top.json()['filas'] if row['producto_id']==catalog['products'][0])
        assert own_product['unidades']==2 and Decimal(own_product['importe'])==Decimal('222')
        assert foreign['products'][0] not in {r['producto_id'] for r in top.json()['filas']}
        weekly=client.post(base+'/dinamicos/ejecutar',json=plan|{'tipo':'ventas_semana'})
        assert weekly.status_code==200
        assert sum(Decimal(row['importe']) for row in weekly.json()['filas'])==total_before+Decimal('222')
        low=client.post(base+'/dinamicos/ejecutar',json={'tipo':'existencias_bajas','nroSuc':catalog['branch'],'umbral':5,'limite':100})
        assert low.status_code==200
        own_stock=next(r for r in low.json()['filas'] if r['variante']==catalog['variants'][0])
        assert own_stock['stock_fisico']==5 and own_stock['disponible']==5
        assert client.post(base+'/dinamicos/ejecutar',json=plan|{'nroSuc':foreign['branch']}).status_code==404
        assert client.post(base+'/dinamicos/ejecutar',json=plan|{'idemp':foreign['tenant']}).status_code==422
        assert client.post(base+'/dinamicos/ejecutar',json=plan|{'tipo':'SELECT * FROM usuario'}).status_code==422
        assert client.post(base+'/dinamicos/ejecutar',json=plan|{'limite':101}).status_code==422
        result_id=top.json()['resultadoId']
        export_url=f'{base}/dinamicos/{result_id}/exportar'
        pdf=client.get(export_url,params={'formato':'pdf'})
        excel=client.get(export_url,params={'formato':'xlsx'})
        assert pdf.status_code==200 and pdf.content.startswith(b'%PDF')
        assert excel.status_code==200
        with ZipFile(BytesIO(excel.content)) as archive:
            sheet=archive.read('xl/worksheets/sheet1.xml').decode()
            assert '222' in sheet and catalog['products'][0] in sheet
        _login_report_user(client,autonomo_report_admins[code][1])
        assert client.get(export_url,params={'formato':'xlsx'}).status_code==404
        _login_report_user(client,autonomo_report_admins[other][0])
        assert client.get(export_url,params={'formato':'pdf'}).status_code==404
        _login_owned(client,reservas_clientes[code][0])
        assert client.post(base+'/dinamicos/ejecutar',json=plan).status_code==403
        client.cookies.clear()
        assert client.get(base+'/dinamicos/referencias').status_code==401


def test_autonomo_reportes_plan_basico_bloquea_ia_antes_del_modelo(autonomo_report_admins):
    prediction={'category':'camiseta','day_of_week':5,'month':6,'price':45.0,'discount':0.2,'promotion':1}
    with TestClient(create_app(_configuracion('cu25')),raise_server_exceptions=False) as client:
        client.headers.update(CABECERAS)
        _login_report_user(client,autonomo_report_admins['urbanwomen'][0])
        assert client.post('/api/admin/reportes/interpretar',json={'texto':'Ventas del mes'}).status_code==403
        assert client.get('/api/admin/reportes/analitica').status_code==403
        assert client.post('/api/admin/reportes/analitica/predecir',json=prediction).status_code==403
        _login_report_user(client,autonomo_report_admins['manaco'][0])
        assert client.post('/api/admin/reportes/interpretar',json={'texto':'Ventas del mes'}).status_code==503
        result=client.get('/api/admin/reportes/analitica')
        assert result.status_code==200 and result.json()['synthetic'] is True
        predicted=client.post('/api/admin/reportes/analitica/predecir',json=prediction)
        assert predicted.status_code==200 and predicted.json()['synthetic'] is True
        assert predicted.json()['predicted_daily_units']>=0
        assert client.post('/api/admin/reportes/analitica/predecir',json=prediction|{'idemp':1}).status_code==422


def _lote(uuid, sucursal: int, variante: str, precio, cantidad: int = 1,
          metodo: str = "efectivo", depende=(), tipo: str = "venta") -> dict:
    if tipo == "venta":
        datos = {"lineas": [{"idVariante": variante, "cantidad": cantidad, "precio": float(precio)}],
                 "metodo": metodo}
    else:
        datos = {"lineas": [{"idVariante": variante, "cantidad": cantidad}],
                 "fecha": "2026-12-01", "hora": "10:00:00"}
    return {
        "dispositivo": "pytest-caja-1",
        "origen": "pwa",
        "operaciones": [{
            "id": str(uuid), "tipo": tipo, "version": 1,
            "fecha": "2026-11-01T10:00:00Z", "sucursal": sucursal,
            "datos": datos, "dependeDe": [str(d) for d in depende],
        }],
    }


def _ventas(plataforma, idemp: int, uuid) -> int:
    with plataforma.connect() as conexion:
        return conexion.execute(text(
            "SELECT count(*) FROM venta WHERE idEmp = :idemp AND idOperacion = CAST(:uuid AS uuid)"),
            {"idemp": idemp, "uuid": str(uuid)}).scalar_one()


def _efectos_venta(plataforma, idemp: int, uuid) -> dict[str, int]:
    with plataforma.connect() as conexion:
        row = conexion.execute(text("""
            SELECT count(DISTINCT v.nroVenta)::integer AS ventas,
                   count(DISTINCT p.nroVenta)::integer AS pagos,
                   count(DISTINCT d.idDetalleVenta)::integer AS detalles,
                   count(DISTINCT m.idMov)::integer AS movimientos
            FROM venta v
            LEFT JOIN pago p ON p.idEmp=v.idEmp AND p.nroVenta=v.nroVenta
            LEFT JOIN detalleVenta d ON d.idEmp=v.idEmp AND d.nroVenta=v.nroVenta
            LEFT JOIN movimiento_inv m ON m.idEmp=v.idEmp
              AND m.motivo LIKE '%' || ' #' || v.nroVenta::varchar
            WHERE v.idEmp=:idemp AND v.idOperacion=CAST(:uuid AS uuid)
        """), {"idemp": idemp, "uuid": str(uuid)}).mappings().one()
        return dict(row)


def _stock(plataforma, idemp: int, sucursal: int, variante: str) -> int:
    with plataforma.connect() as conexion:
        return conexion.execute(text("""
            SELECT cantDisp FROM inventario
            WHERE idEmp = :idemp AND nroSuc = :sucursal AND idVar = :variante
        """), {"idemp": idemp, "sucursal": sucursal, "variante": variante}).scalar_one()


def test_cu24_multitenant_sucursal_auditoria_inventario_y_pago(
        client, credenciales, plataforma, datos):
    """Venta offline CU24: identidad, referencias, UUID y efectos SaaS atómicos."""
    demo = {empresa["codigo"]: empresa for empresa in credenciales["empresas"]}
    cashier = demo["manaco"]["usuarios"]["cajero"]
    manaco = datos("manaco")
    # Falla como error de preparación si alguien apunta a una base legacy; no
    # convierte el desfase en un skip de la cobertura de CU24.
    with plataforma.connect() as conexion:
        schema = conexion.execute(text("""
            SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public'
                           AND table_name='venta' AND column_name='idoperacion' AND udt_name='uuid')
               AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public'
                           AND table_name='operacion_sync' AND column_name='payload_hash')
        """)).scalar_one()
    assert schema, "fashionstore_test no tiene el esquema SaaS requerido para sync/CU24"
    assert _entrar_empresa(client, credenciales, "manaco", "cajero").status_code == 200
    assert "CU24" in client.get("/api/auth/me").json()["permisos"]
    assert client.get("/api/admin/caja/referencias").status_code == 503

    # Una vez verificado el camino de escritura por sync, el allowlist expone
    # solo lecturas de caja que conservan RLS y el scope de sucursal.
    caja_app = create_app(_configuracion("cu09,sync,saas,cu24"))
    with TestClient(caja_app, raise_server_exceptions=False) as caja:
        caja.headers.update(CABECERAS)
        assert _entrar_empresa(caja, credenciales, "manaco", "cajero").status_code == 200
        refs = caja.get("/api/admin/caja/referencias")
        assert refs.status_code == 200, refs.text
        assert refs.json()["sucursalAsignada"] == manaco["sucursal"]
        assert {row["nro"] for row in refs.json()["sucursales"]} == {manaco["sucursal"]}
        products = caja.get("/api/admin/caja/productos", params={"limit": 100})
        assert products.status_code == 200, products.text
        with plataforma.connect() as conexion:
            foreign_product = conexion.execute(text("""
                SELECT p.idProd FROM empresa e JOIN producto p ON p.idEmp=e.idEmp
                WHERE e.codigo='lupo' ORDER BY p.idProd LIMIT 1
            """)).scalar_one()
        assert foreign_product not in {row["idProd"] for row in products.json()["items"]}
        assert caja.get(f"/api/admin/caja/productos/{foreign_product}").status_code == 404
        assert caja.get("/api/admin/caja/clientes", params={
            "idUsuario": demo["lupo"]["usuarios"]["cliente"]["idUsuario"]
        }).json() == []
        assert caja.get("/api/admin/caja/1").status_code == 503
        assert caja.post("/api/admin/caja", json={}).status_code == 503

    operation = uuid4()
    lot = _lote(operation, manaco["sucursal"], manaco["idVariante"], manaco["precio"])
    stock_before = _stock(plataforma, manaco["idEmp"], manaco["sucursal"], manaco["idVariante"])
    first = client.post("/api/sync/lote", json=lot)
    assert first.status_code == 200, first.text
    result = first.json()["resultados"][0]
    assert result["estado"] == "sincronizada", result
    sale_no = result["nroVenta"]
    assert sale_no and _ventas(plataforma, manaco["idEmp"], operation) == 1
    assert _stock(plataforma, manaco["idEmp"], manaco["sucursal"], manaco["idVariante"]) == stock_before - 1
    assert _efectos_venta(plataforma, manaco["idEmp"], operation) == {
        "ventas": 1, "pagos": 1, "detalles": 1, "movimientos": 1,
    }
    with plataforma.connect() as conexion:
        effects = conexion.execute(text("""
            SELECT v.idOperacion::text AS "idOperacion", s.estado,
                   (SELECT count(*) FROM pago p WHERE p.idEmp=v.idEmp AND p.nroVenta=v.nroVenta
                    AND p.estado='aprobado') AS pagos_aprobados,
                   (SELECT count(*) FROM bitacora b WHERE b.idEmp=v.idEmp AND b.usuario_id=v.idUsuarioEmp
                    AND b.accion='venta_offline_confirmada'
                    AND b.detalles->>'idOperacion'=v.idOperacion::text
                    AND (b.detalles->>'nroVenta')::integer=v.nroVenta) AS auditorias
            FROM venta v JOIN operacion_sync s ON s.idEmp=v.idEmp AND s.idOperacion=v.idOperacion
            WHERE v.idEmp=:idemp AND v.nroVenta=:sale
        """), {"idemp": manaco["idEmp"], "sale": sale_no}).mappings().one()
    assert effects["idOperacion"] == str(operation)
    assert effects["estado"] == "confirmada"
    assert effects["pagos_aprobados"] == effects["auditorias"] == 1

    replay = client.post("/api/sync/lote", json=lot).json()["resultados"][0]
    assert replay["estado"] == "sincronizada" and replay["idempotente"]
    assert replay["nroVenta"] == sale_no
    assert _stock(plataforma, manaco["idEmp"], manaco["sucursal"], manaco["idVariante"]) == stock_before - 1

    altered = _lote(operation, manaco["sucursal"], manaco["idVariante"], manaco["precio"], cantidad=2)
    incompatible = client.post("/api/sync/lote", json=altered).json()["resultados"][0]
    assert (incompatible["estado"], incompatible["codigo"]) == ("conflicto", "contenido_incompatible")
    assert _ventas(plataforma, manaco["idEmp"], operation) == 1

    # La variante exclusiva de otro tenant no se resuelve bajo el tenant de Manaco.
    with plataforma.connect() as conexion:
        foreign_variant = conexion.execute(text("""
            SELECT v.idVariante AS "idVariante", v.precio FROM empresa e JOIN varianteProd v ON v.idEmp=e.idEmp
            WHERE e.codigo='lupo' ORDER BY v.idVariante LIMIT 1
        """)).one()
        unassigned_branch = conexion.execute(text("""
            SELECT nro FROM sucursal WHERE idEmp=:idemp AND estado='activo' AND nro<>:assigned LIMIT 1
        """), {"idemp": manaco["idEmp"], "assigned": manaco["sucursal"]}).scalar_one()
    foreign_uuid = uuid4()
    rejected_foreign = client.post("/api/sync/lote", json=_lote(
        foreign_uuid, manaco["sucursal"], foreign_variant.idVariante, foreign_variant.precio)).json()["resultados"][0]
    assert rejected_foreign["estado"] in {"conflicto", "rechazada"}
    assert _efectos_venta(plataforma, manaco["idEmp"], foreign_uuid) == {
        "ventas": 0, "pagos": 0, "detalles": 0, "movimientos": 0,
    }

    wrong_branch_uuid = uuid4()
    rejected_branch = client.post("/api/sync/lote", json=_lote(
        wrong_branch_uuid, unassigned_branch, manaco["idVariante"], manaco["precio"])).json()["resultados"][0]
    assert rejected_branch["estado"] == "rechazada"
    assert rejected_branch["codigo"] == "sucursal_invalida"
    assert _efectos_venta(plataforma, manaco["idEmp"], wrong_branch_uuid) == {
        "ventas": 0, "pagos": 0, "detalles": 0, "movimientos": 0,
    }

    # A client identity cannot pass the CU24 permission gate.
    assert _entrar_empresa(client, credenciales, "manaco", "cliente").status_code == 200
    denied = client.post("/api/sync/lote", json=_lote(
        uuid4(), manaco["sucursal"], manaco["idVariante"], manaco["precio"])).json()["resultados"][0]
    assert (denied["estado"], denied["codigo"]) == ("rechazada", "permiso_denegado")


# ---------------------------------------------------------------------------
# Autenticación de plataforma
# ---------------------------------------------------------------------------

def test_login_plataforma_y_consola_saas(client, credenciales):
    respuesta = _entrar_plataforma(client, credenciales)
    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["plataforma"] is True
    assert cuerpo["empresa"] is None
    assert set(cuerpo["permisos"]) == PERMISOS_SAAS

    empresas = client.get("/api/saas/empresas")
    assert empresas.status_code == 200, empresas.text
    codigos = {empresa["codigo"] for empresa in empresas.json()}
    assert codigos == set(EMPRESAS)
    assert all(empresa["esDemostracion"] for empresa in empresas.json())

    planes = client.get("/api/saas/planes")
    assert planes.status_code == 200
    assert {plan["nombre"] for plan in planes.json()} == {"Basico", "Profesional"}

    assert client.get("/api/saas/auditoria").status_code == 200

    # El archivo local de credenciales solo se sirve con DEMO_MODE + SAAS-DEMO.
    demo = client.get("/api/saas/demo/credenciales")
    assert demo.status_code == 200, demo.text
    assert demo.json()["entorno"].startswith("demostración")


@pytest.fixture
def restaurar_panel_saas(plataforma, datos):
    manaco = datos("manaco")
    with plataforma.connect() as conexion:
        empresa = dict(conexion.execute(text("""
            SELECT nombre, logoUrl, colorPrimario, colorSecundario
            FROM empresa WHERE idEmp=:idemp
        """), {"idemp": manaco["idEmp"]}).mappings().one())
        suscripciones = [dict(row) for row in conexion.execute(text("""
            SELECT idSusc, idPlan, fechaInicio, fechaVencimiento, estado
            FROM suscripcion WHERE idEmp=:idemp
        """), {"idemp": manaco["idEmp"]}).mappings()]
        audit_max = conexion.execute(text("SELECT COALESCE(max(id), 0) FROM bitacora")).scalar_one()
        actor = conexion.execute(text(
            "SELECT idUsuario FROM usuario WHERE tipo='S' AND idEmp IS NULL LIMIT 1")).scalar_one()
    yield
    original_ids = [row["idsusc"] for row in suscripciones]
    with plataforma.begin() as conexion:
        actuales = conexion.execute(text(
            "SELECT idSusc FROM suscripcion WHERE idEmp=:idemp"),
            {"idemp": manaco["idEmp"]}).scalars().all()
        nuevos = [idsusc for idsusc in actuales if idsusc not in original_ids]
        if nuevos:
            conexion.execute(text(
                "DELETE FROM suscripcion_plan_version WHERE idsusc = ANY(CAST(:ids AS integer[]))"),
                {"ids": nuevos})
            conexion.execute(text(
                "DELETE FROM suscripcion WHERE idsusc = ANY(CAST(:ids AS integer[]))"),
                {"ids": nuevos})
        for sub in suscripciones:
            conexion.execute(text("""
                UPDATE suscripcion
                SET idPlan=:plan, fechaInicio=:inicio, fechaVencimiento=:fin, estado=:estado
                WHERE idSusc=:idsusc AND idEmp=:idemp
            """), {"plan": sub["idplan"], "inicio": sub["fechainicio"],
                  "fin": sub["fechavencimiento"], "estado": sub["estado"],
                  "idsusc": sub["idsusc"], "idemp": manaco["idEmp"]})
        conexion.execute(text("""
            UPDATE empresa
            SET nombre=:nombre, logoUrl=:logo, colorPrimario=:primario,
                colorSecundario=:secundario
            WHERE idEmp=:idemp
        """), {"nombre": empresa["nombre"], "logo": empresa["logourl"],
              "primario": empresa["colorprimario"], "secundario": empresa["colorsecundario"],
              "idemp": manaco["idEmp"]})
        conexion.execute(text("""
            DELETE FROM bitacora
            WHERE id > :audit_max AND usuario_id=:actor
              AND accion IN ('empresa_identidad_actualizada', 'empresa_plan_cambiado',
                             'soporte_acceso_empresa')
              AND detalles->>'idEmp'=:idemp
        """), {"audit_max": audit_max, "actor": actor, "idemp": str(manaco["idEmp"])})


def test_panel_saas_planes_identidad_suscripcion_soporte_y_auditoria(
        client, credenciales, restaurar_panel_saas):
    assert _entrar_plataforma(client, credenciales).status_code == 200
    company = next(row for row in client.get("/api/saas/empresas").json() if row["codigo"] == "manaco")
    assert company["idPlan"]

    # La API valida también URLs de logo: no admite esquemas ejecutables ni HTTP.
    invalid = client.patch(f"/api/saas/empresas/{company['idEmp']}/identidad", json={"logoUrl": "javascript:alert(1)"})
    assert invalid.status_code == 422
    assert client.patch(f"/api/saas/empresas/{company['idEmp']}/identidad", json={
        "nombre": company["nombre"], "logoUrl": company["logoUrl"],
        "colorPrimario": company["colorPrimario"], "colorSecundario": company["colorSecundario"],
    }).status_code == 200

    hoy = date.today()
    fin = hoy.replace(year=hoy.year + 1)
    cambiado = client.post(f"/api/saas/empresas/{company['idEmp']}/suscripcion", json={
        "idPlan": company["idPlan"], "fechaInicio": hoy.isoformat(), "fechaVencimiento": fin.isoformat(),
    })
    assert cambiado.status_code == 200, cambiado.text
    assert cambiado.json()["idPlan"] == company["idPlan"]

    soporte = client.post("/api/saas/soporte/acceso", json={
        "idEmp": company["idEmp"], "motivo": "Verificar información de la empresa para la demostración",
    })
    assert soporte.status_code == 200, soporte.text
    assert "solo lectura" in soporte.json()["aviso"].lower()
    assert "credencial" not in soporte.json()

    eventos = client.get("/api/saas/auditoria").json()
    acciones = {event["accion"] for event in eventos}
    assert {"empresa_identidad_actualizada", "empresa_plan_cambiado", "soporte_acceso_empresa"} <= acciones


def test_version_planes_conserva_configuracion_no_seleccionada(plataforma, datos):
    a, b = datos("manaco"), datos("kosy-pantalones")
    motor = create_engine(PLATFORM_URL, hide_parameters=True)
    factory = sessionmaker(motor, expire_on_commit=False)
    try:
        with factory() as db:
            subscriptions = db.execute(text("""
                SELECT e.codigo, s.idsusc, s.idplan, sv.idversion
                FROM empresa e
                JOIN suscripcion s ON s.idemp=e.idemp AND s.estado='activa'
                JOIN suscripcion_plan_version sv ON sv.idsusc=s.idsusc AND sv.idplan=s.idplan
                WHERE e.idemp IN (:a, :b)
                  AND CURRENT_DATE BETWEEN s.fechainicio AND s.fechavencimiento
                ORDER BY e.idemp
            """), {"a": a["idEmp"], "b": b["idEmp"]}).mappings().all()
            assert {row["codigo"] for row in subscriptions} == {"manaco", "kosy-pantalones"}
            assert len({row["idplan"] for row in subscriptions}) == 1
            plan_id = subscriptions[0]["idplan"]
            selected = next(row for row in subscriptions if row["codigo"] == "manaco")
            preserved = next(row for row in subscriptions if row["codigo"] == "kosy-pantalones")
            original_plan = db.get(Plan, plan_id)
            original_plan_state = (original_plan.nombre, original_plan.maxsucursales, original_plan.estado)
            original_plan_features = list(db.scalars(
                select(PlanFuncionalidad.codigo).where(PlanFuncionalidad.idplan == plan_id)))
            original_versions = [{
                "idversion": version.idversion, "vigente": version.vigente,
                "nombre": version.nombre, "maxsucursales": version.maxsucursales,
                "estado": version.estado,
            } for version in db.scalars(select(PlanVersion).where(PlanVersion.idplan == plan_id))]
            original_mappings = list(db.execute(select(
                SuscripcionVersion.idsusc, SuscripcionVersion.idversion,
            ).where(SuscripcionVersion.idplan == plan_id)))
            original_selected = planes_service.subscription_version(db, selected["idsusc"])
            original_preserved = planes_service.subscription_version(db, preserved["idsusc"])
            original_audit_max = db.scalar(text("SELECT COALESCE(max(id), 0) FROM bitacora"))
            actor = db.scalar(text(
                "SELECT idUsuario FROM usuario WHERE tipo='S' AND idEmp IS NULL LIMIT 1"))

        proposed_features = original_selected["funcionalidades"][1:]
        proposed_limit = original_selected["maxSucursales"] + 1
        data = PlanActualizacion(
            nombre=original_plan_state[0], maxSucursales=proposed_limit,
            estado=original_plan_state[2], funcionalidades=proposed_features,
            suscripcionesSeleccionadas=[selected["idsusc"]],
        )
        try:
            with factory() as db:
                impact = planes_service.preview(db, plan_id, data)
                data = data.model_copy(update={"confirmacionImpacto": impact["confirmacionImpacto"]})
                planes_service.update(db, plan_id, data, actor, "127.0.0.1")
                changed = planes_service.subscription_version(db, selected["idsusc"])
                unchanged = planes_service.subscription_version(db, preserved["idsusc"])
                assert changed["version"] == original_selected["version"] + 1
                assert changed["maxSucursales"] == proposed_limit
                assert changed["funcionalidades"] == proposed_features
                assert unchanged == original_preserved
        finally:
            with plataforma.begin() as conexion:
                for mapping in original_mappings:
                    conexion.execute(text("""
                        UPDATE suscripcion_plan_version
                        SET idversion=:idversion
                        WHERE idsusc=:idsusc AND idplan=:idplan
                    """), {"idversion": mapping.idversion, "idsusc": mapping.idsusc,
                          "idplan": plan_id})
                previous_ids = [row["idversion"] for row in original_versions]
                new_ids = conexion.execute(text("""
                    SELECT idversion FROM plan_version
                    WHERE idplan=:idplan AND NOT (idversion = ANY(CAST(:old_ids AS integer[])))
                """), {"idplan": plan_id, "old_ids": previous_ids}).scalars().all()
                if new_ids:
                    conexion.execute(text(
                        "UPDATE plan_version SET vigente=false WHERE idversion = ANY(CAST(:ids AS integer[]))"),
                        {"ids": new_ids})
                for version in original_versions:
                    if version["vigente"]:
                        conexion.execute(text(
                            "UPDATE plan_version SET vigente=true WHERE idversion=:idversion"),
                            {"idversion": version["idversion"]})
                for version_id in new_ids:
                    conexion.execute(text(
                        "DELETE FROM plan_version_funcionalidad WHERE idversion=:idversion"),
                        {"idversion": version_id})
                    conexion.execute(text(
                        "DELETE FROM plan_version WHERE idversion=:idversion"),
                        {"idversion": version_id})
                for version in original_versions:
                    conexion.execute(text("""
                        UPDATE plan_version
                        SET nombre=:nombre, maxsucursales=:maxsucursales,
                            estado=:estado
                        WHERE idversion=:idversion
                    """), {"nombre": version["nombre"], "maxsucursales": version["maxsucursales"],
                          "estado": version["estado"],
                          "idversion": version["idversion"]})
                conexion.execute(text("""
                    UPDATE plan SET nombre=:nombre, maxSucursales=:maxsucursales, estado=:estado
                    WHERE idplan=:idplan
                """), {"nombre": original_plan_state[0], "maxsucursales": original_plan_state[1],
                      "estado": original_plan_state[2], "idplan": plan_id})
                conexion.execute(text("DELETE FROM plan_funcionalidad WHERE idplan=:idplan"),
                                 {"idplan": plan_id})
                for code in original_plan_features:
                    conexion.execute(text(
                        "INSERT INTO plan_funcionalidad(idplan, codigo) VALUES (:idplan, :codigo)"),
                        {"idplan": plan_id, "codigo": code})
                conexion.execute(text("""
                    DELETE FROM bitacora
                    WHERE id > :audit_max AND usuario_id=:actor
                      AND accion='plan_saas_actualizado' AND detalles->>'plan'=:plan
                """), {"audit_max": original_audit_max, "actor": actor, "plan": str(plan_id)})
    finally:
        motor.dispose()


def test_version_plan_rechaza_reduccion_incompatible(plataforma, datos):
    manaco = datos("manaco")
    motor = create_engine(PLATFORM_URL, hide_parameters=True)
    factory = sessionmaker(motor, expire_on_commit=False)
    try:
        with factory() as db:
            subscription = db.scalar(select(SuscripcionPlan).where(
                SuscripcionPlan.idemp == manaco["idEmp"], SuscripcionPlan.estado == "activa"))
            assert subscription is not None
            branches = db.scalar(text(
                "SELECT count(*) FROM sucursal WHERE idEmp=:idemp AND estado='activo'"),
                {"idemp": manaco["idEmp"]})
            assert branches > 1, "Fixture Manaco no permite probar reducción incompatible"
            version = planes_service.subscription_version(db, subscription.idsusc)
            data = PlanActualizacion(
                nombre=version["nombre"], maxSucursales=1, estado=version["estado"],
                funcionalidades=version["funcionalidades"],
                suscripcionesSeleccionadas=[subscription.idsusc],
            )
            with pytest.raises(DomainError) as error:
                planes_service.preview(db, subscription.idplan, data)
            assert error.value.code == "limite_incompatible"
    finally:
        motor.dispose()


def test_version_plan_actualizacion_es_atomica_ante_conflicto(plataforma, datos):
    manaco = datos("manaco")
    motor = create_engine(PLATFORM_URL, hide_parameters=True)
    factory = sessionmaker(motor, expire_on_commit=False)
    try:
        with factory() as db:
            subscription = db.scalar(select(SuscripcionPlan).where(
                SuscripcionPlan.idemp == manaco["idEmp"], SuscripcionPlan.estado == "activa"))
            subscription_id = subscription.idsusc
            plan_id = subscription.idplan
            plan = db.get(Plan, plan_id)
            other_name = db.scalar(select(Plan.nombre).where(Plan.idplan != plan.idplan).limit(1))
            original = (plan.nombre, plan.maxsucursales, plan.estado)
            version_count = db.scalar(select(func.count()).select_from(PlanVersion).where(
                PlanVersion.idplan == plan.idplan))
            mapping = db.get(SuscripcionVersion, subscription_id).idversion
            features = list(db.scalars(select(PlanFuncionalidad.codigo).where(
                PlanFuncionalidad.idplan == plan.idplan)))
            data = PlanActualizacion(
                nombre=other_name, maxSucursales=plan.maxsucursales, estado=plan.estado,
                funcionalidades=features, suscripcionesSeleccionadas=[],
            )
            impact = planes_service.preview(db, plan.idplan, data)
            data = data.model_copy(update={"confirmacionImpacto": impact["confirmacionImpacto"]})
            with pytest.raises(DomainError):
                planes_service.update(db, plan.idplan, data, "e2e-atomicity", "127.0.0.1")
        with factory() as db:
            current = db.get(Plan, plan_id)
            assert (current.nombre, current.maxsucursales, current.estado) == original
            assert db.scalar(select(func.count()).select_from(PlanVersion).where(
                PlanVersion.idplan == current.idplan)) == version_count
            assert db.get(SuscripcionVersion, subscription_id).idversion == mapping
    finally:
        motor.dispose()


def test_limite_sucursales_concurrente_no_supera_version(plataforma, datos):
    manaco = datos("manaco")
    names = [f"concurrencia-{uuid4().hex[:12]}" for _ in range(2)]
    branch_numbers: list[int] = []
    test_version_id = None
    subscription_id = None
    original_version_id = None
    motor = create_engine(PLATFORM_URL, hide_parameters=True)
    factory = sessionmaker(motor)
    try:
        with factory() as db:
            subscriptions = db.execute(text("""
                SELECT s.idsusc, s.idplan, sv.idversion, v.maxsucursales, v.nombre, v.estado
                FROM suscripcion s
                JOIN suscripcion_plan_version sv ON sv.idsusc=s.idsusc AND sv.idplan=s.idplan
                JOIN plan_version v ON v.idversion=sv.idversion AND v.idplan=s.idplan
                WHERE s.idemp=:idemp AND s.estado='activa'
                  AND CURRENT_DATE BETWEEN s.fechainicio AND s.fechavencimiento
            """), {"idemp": manaco["idEmp"]}).mappings().all()
            assert len(subscriptions) == 1, "Manaco debe tener una sola suscripción vigente para la prueba"
            subscription_id = subscriptions[0]["idsusc"]
            original_version_id = subscriptions[0]["idversion"]
            branches, city_id, next_number = db.execute(text("""
                SELECT count(*) FILTER (WHERE estado='activo'),
                       (SELECT min(id) FROM ciudad WHERE idemp=:idemp),
                       (SELECT coalesce(max(nro), 0) + 1 FROM sucursal)
                FROM sucursal WHERE idemp=:idemp
            """), {"idemp": manaco["idEmp"]}).one()
            assert city_id is not None
            branch_numbers = [next_number, next_number + 1]
            cap = branches + 1
            assert 0 < cap <= 32767

        with plataforma.begin() as conexion:
            test_version_id = conexion.execute(text("""
                INSERT INTO plan_version(idplan, numero, nombre, maxsucursales, estado, vigente)
                SELECT s.idplan,
                       (SELECT max(numero)+1 FROM plan_version WHERE idplan=s.idplan),
                       v.nombre, :limite, v.estado, false
                FROM suscripcion s
                JOIN plan_version v ON v.idversion=:version_actual
                WHERE s.idsusc=:idsusc
                RETURNING idversion
            """), {"limite": cap, "idsusc": subscription_id,
                  "version_actual": original_version_id}).scalar_one()
            conexion.execute(text("""
                INSERT INTO plan_version_funcionalidad(idversion, codigo)
                SELECT :version_nueva, codigo
                FROM plan_version_funcionalidad WHERE idversion=:version_actual
            """), {"version_nueva": test_version_id, "version_actual": original_version_id})
            conexion.execute(text("""
                UPDATE suscripcion_plan_version SET idversion=:version_nueva
                WHERE idsusc=:idsusc
            """), {"version_nueva": test_version_id, "idsusc": subscription_id})

        barrier = threading.Barrier(2)
        postgres_url = make_url(APP_URL).set(drivername="postgresql").render_as_string(
            hide_password=False)

        def intentar_alta(index: int):
            barrier.wait(timeout=10)
            try:
                with psycopg.connect(postgres_url) as conexion:
                    conexion.execute("SELECT set_config('app.idemp', %s, true)",
                                     (str(manaco["idEmp"]),))
                    conexion.execute("""
                        INSERT INTO sucursal(nro, nombre, direccion, estado, idciud, idemp)
                        VALUES (%s, %s, 'Prueba concurrente aislada', 'activo', %s, %s)
                    """, (branch_numbers[index], names[index], city_id, manaco["idEmp"]))
                return True, None, None
            except psycopg.Error as error:
                return False, error.sqlstate, error.diag.message_primary

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(intentar_alta, range(2)))
        successes = [result for result in results if result[0]]
        rejected = [result for result in results if not result[0]]
        assert len(successes) == 1, f"Se esperó una sola sucursal aceptada; resultados: {results}"
        assert len(rejected) == 1
        assert rejected[0][1] == "P0001"
        assert "Límite de sucursales activas del plan alcanzado" in rejected[0][2]
    finally:
        if test_version_id is not None:
            with plataforma.begin() as conexion:
                conexion.execute(text("""
                    DELETE FROM sucursal
                    WHERE idemp=:idemp AND nro=ANY(CAST(:numeros AS smallint[]))
                      AND nombre=ANY(CAST(:nombres AS varchar[]))
                """), {"idemp": manaco["idEmp"], "numeros": branch_numbers, "nombres": names})
                if original_version_id is not None and subscription_id is not None:
                    conexion.execute(text("""
                        UPDATE suscripcion_plan_version SET idversion=:version_actual
                        WHERE idsusc=:idsusc
                    """), {"version_actual": original_version_id, "idsusc": subscription_id})
                conexion.execute(text(
                    "DELETE FROM plan_version_funcionalidad WHERE idversion=:idversion"),
                    {"idversion": test_version_id})
                conexion.execute(text(
                    "DELETE FROM plan_version WHERE idversion=:idversion"),
                    {"idversion": test_version_id})
        motor.dispose()


@pytest.fixture
def empleados_cu05_temporales(plataforma):
    """Limpia las cuentas creadas por el recorrido CU05, preservando bitácora."""
    creados: list[tuple[int, str]] = []
    yield creados
    with plataforma.begin() as conexion:
        for idemp, correo in creados:
            conexion.execute(text(
                "DELETE FROM usuario WHERE idEmp=:idemp AND lower(btrim(correo))=:correo"),
                {"idemp": idemp, "correo": correo})


def test_cu07_clientes_aislamiento_permisos_y_auditoria(
        client, cu07_client, credenciales, plataforma, clientes_temporales_cu07):
    base = "/api/admin/clientes"
    assert _entrar_empresa(client, credenciales, "manaco", "admin").status_code == 200
    assert client.get(base).status_code == 503
    assert cu07_client.get(base).status_code == 401

    manaco = next(row for row in credenciales["empresas"] if row["codigo"] == "manaco")
    urbanwomen = next(row for row in credenciales["empresas"] if row["codigo"] == "urbanwomen")
    email = f"cu07-{uuid4().hex}@example.com"

    assert _entrar_empresa(cu07_client, credenciales, "manaco", "admin").status_code == 200
    identidad = cu07_client.get("/api/auth/me").json()
    assert identidad["empresa"] == "manaco" and "CU07" in identidad["permisos"]
    assert cu07_client.get("/api/admin/usuarios").status_code == 503

    manaco_response = cu07_client.post(base, json=_cliente_cu07(email))
    assert manaco_response.status_code == 201, manaco_response.text
    manaco_user = manaco_response.json()
    manaco_id = manaco_user["idUsuario"]
    clientes_temporales_cu07.append((manaco["idEmp"], manaco_id))
    assert cu07_client.get(base, params={"q": email}).json()["items"][0]["idUsuario"] == manaco_id
    assert cu07_client.get("/api/auth/me").json()["empresa"] == "manaco"

    edited = cu07_client.patch(f"{base}/{manaco_id}", json={"telefono": "70000001"})
    assert edited.status_code == 200 and edited.json()["telefono"] == "70000001"
    disabled = cu07_client.patch(f"{base}/{manaco_id}/estado-cuenta", json={"activo": False})
    assert disabled.status_code == 200 and disabled.json()["cliente"]["estado"] == "inactivo"

    with TestClient(create_app(_configuracion("cu07")), raise_server_exceptions=False) as customer:
        customer.headers.update(CABECERAS)
        assert _entrar_empresa(customer, credenciales, "manaco", "cliente").status_code == 200
        assert "CU07" not in customer.get("/api/auth/me").json()["permisos"]
        assert customer.get(base).status_code == 403

    assert _entrar_empresa(cu07_client, credenciales, "urbanwomen", "admin").status_code == 200
    assert cu07_client.get("/api/auth/me").json()["empresa"] == "urbanwomen"
    urbanwomen_response = cu07_client.post(base, json=_cliente_cu07(email))
    assert urbanwomen_response.status_code == 201, urbanwomen_response.text
    urbanwomen_id = urbanwomen_response.json()["idUsuario"]
    clientes_temporales_cu07.append((urbanwomen["idEmp"], urbanwomen_id))
    assert urbanwomen_id != manaco_id
    assert cu07_client.get(base, params={"q": email}).json()["items"][0]["idUsuario"] == urbanwomen_id

    assert cu07_client.get(f"{base}/{manaco_id}").status_code == 404
    assert cu07_client.patch(f"{base}/{manaco_id}", json={"telefono": "cruce"}).status_code == 404
    assert cu07_client.patch(
        f"{base}/{manaco_id}/estado-cuenta", json={"activo": False}).status_code == 404

    with plataforma.connect() as conexion:
        manaco_auditoria = conexion.execute(text("""
            SELECT accion FROM bitacora
            WHERE idEmp=:idemp AND usuario_id=:actor
              AND detalles->>'idUsuario'=:resource
        """), {"idemp": manaco["idEmp"],
              "actor": identidad["usuario"]["idUsuario"], "resource": manaco_id}).scalars().all()
        assert set(manaco_auditoria) >= {"cliente_registrado", "cliente_actualizado", "cliente_desactivado"}
        owners = conexion.execute(text("""
            SELECT idEmp FROM usuario WHERE idUsuario IN (:manaco, :urbanwomen)
            ORDER BY idEmp
        """), {"manaco": manaco_id, "urbanwomen": urbanwomen_id}).scalars().all()
        assert owners == [manaco["idEmp"], urbanwomen["idEmp"]]


def test_cu07_rollback_de_cliente_y_auditoria(
        cu07_client, credenciales, plataforma, clientes_temporales_cu07, monkeypatch):
    assert _entrar_empresa(cu07_client, credenciales, "manaco", "admin").status_code == 200
    company = next(row for row in credenciales["empresas"] if row["codigo"] == "manaco")
    actor = cu07_client.get("/api/auth/me").json()["usuario"]["idUsuario"]
    email = f"cu07-rollback-{uuid4().hex}@example.com"
    created_ids = []
    original_record = cu07_cliente_service.record

    def fail_after_audit(*args, **kwargs):
        created_ids.append(kwargs["resource_id"])
        clientes_temporales_cu07.append((company["idEmp"], kwargs["resource_id"]))
        original_record(*args, **kwargs)
        raise RuntimeError("fallo inyectado después del INSERT de auditoría")

    monkeypatch.setattr(cu07_cliente_service, "record", fail_after_audit)
    response = cu07_client.post("/api/admin/clientes", json=_cliente_cu07(email))
    assert response.status_code == 500
    assert len(created_ids) == 1
    with plataforma.connect() as conexion:
        assert conexion.execute(text("""
            SELECT count(*) FROM usuario
            WHERE idEmp=:idemp AND lower(btrim(correo))=:correo
        """), {"idemp": company["idEmp"], "correo": email}).scalar_one() == 0
        assert conexion.execute(text("""
            SELECT count(*) FROM bitacora
            WHERE idEmp=:idemp AND usuario_id=:actor
              AND detalles->>'idUsuario'=:resource
        """), {"idemp": company["idEmp"], "actor": actor,
              "resource": created_ids[0]}).scalar_one() == 0


def test_cu05_empleados_multitenant_crud_referencias_permisos_y_auditoria(
        client, credenciales, plataforma, empleados_cu05_temporales):
    """CU05 funcional para dos tenants; fallos de tenant/refs no persisten."""
    # El módulo permanece cerrado por defecto. Se abre solo en esta app aislada.
    assert _entrar_empresa(client, credenciales, "manaco", "admin").status_code == 200
    assert client.get("/api/admin/usuarios").status_code == 503

    app = create_app(_configuracion("cu05,cu09,sync,saas,cu24"))
    with TestClient(app, raise_server_exceptions=False) as admin:
        admin.headers.update(CABECERAS)
        created_by_company: dict[str, tuple[str, str, int]] = {}

        for codigo in ("manaco", "lupo"):
            company = next(row for row in credenciales["empresas"] if row["codigo"] == codigo)
            idemp = company["idEmp"]
            assert _entrar_empresa(admin, credenciales, codigo, "admin").status_code == 200
            identity = admin.get("/api/auth/me").json()
            assert identity["empresa"] == codigo and "CU05" in identity["permisos"]

            roles = admin.get("/api/admin/usuarios/roles")
            branches = admin.get("/api/admin/empleados/sucursales")
            assert roles.status_code == branches.status_code == 200
            cashier_role = next(role["nro"] for role in roles.json() if role["nro"].endswith("-CAJ"))
            branch = branches.json()[0]
            actor_id = identity["usuario"]["idUsuario"]
            correo = f"cu05-{codigo}-{uuid4().hex}@example.com"
            empleados_cu05_temporales.append((idemp, correo))
            payload = {
                "ci": f"CU05-{uuid4().hex[:12]}", "nombre": "Empleado Prueba",
                "apellidoPat": "Tenant", "apellidoMat": "Aislado", "sexo": "F",
                "correo": correo, "telefono": "70000000", "direccion": "Sucursal de prueba",
                "fechaNac": "1998-02-03", "contrasena": "Prueba-CU05-2026!",
                "nroRol": cashier_role, "cargo": "Cajero", "nroSuc": branch["nro"],
            }
            before_audit = plataforma.connect()
            try:
                audit_before = before_audit.execute(text(
                    "SELECT count(*) FROM bitacora WHERE idEmp=:idemp AND usuario_id=:actor"),
                    {"idemp": idemp, "actor": actor_id}).scalar_one()
            finally:
                before_audit.close()

            created = admin.post("/api/admin/empleados", json=payload)
            assert created.status_code == 201, created.text
            employee = created.json()
            employee_id = employee["idUsuario"]
            assert employee["empleado"]["nroSuc"] == branch["nro"]
            assert employee["nroRol"] == cashier_role
            assert "contrasena" not in employee
            created_by_company[codigo] = (employee_id, cashier_role, branch["nro"])

            listed = admin.get("/api/admin/usuarios", params={"q": correo, "tipo": "E"})
            assert listed.status_code == 200
            assert listed.json()["total"] == 1
            assert listed.json()["items"][0]["idUsuario"] == employee_id
            assert "contrasena" not in listed.text

            edited = admin.patch(f"/api/admin/empleados/{employee_id}", json={
                "cargo": "Encargado de pruebas", "direccion": "Dirección actualizada"})
            assert edited.status_code == 200, edited.text
            assert edited.json()["empleado"]["cargo"] == "Encargado de pruebas"
            disabled = admin.patch(f"/api/admin/empleados/{employee_id}/estado-cuenta",
                                   json={"activo": False})
            assert disabled.status_code == 200 and disabled.json()["estado"] == "inactivo"
            enabled = admin.patch(f"/api/admin/empleados/{employee_id}/estado-cuenta",
                                  json={"activo": True})
            assert enabled.status_code == 200 and enabled.json()["estado"] == "activo"

            with plataforma.connect() as conexion:
                audit_after = conexion.execute(text(
                    "SELECT count(*) FROM bitacora WHERE idEmp=:idemp AND usuario_id=:actor"),
                    {"idemp": idemp, "actor": actor_id}).scalar_one()
                assert audit_after >= audit_before + 6
                assert conexion.execute(text("""
                    SELECT count(*) FROM bitacora
                    WHERE idEmp=:idemp AND usuario_id=:actor
                      AND accion IN ('usuario_creado','empleado_creado','usuario_actualizado',
                                     'empleado_actualizado','usuario_desactivado','usuario_activado')
                """), {"idemp": idemp, "actor": actor_id}).scalar_one() >= 6

        # Tenant Manaco no puede consultar ni editar el empleado de Lupo.
        assert _entrar_empresa(admin, credenciales, "manaco", "admin").status_code == 200
        lupo_employee = created_by_company["lupo"][0]
        assert admin.get(f"/api/admin/usuarios/{lupo_employee}").status_code == 404
        assert admin.patch(f"/api/admin/empleados/{lupo_employee}",
                           json={"cargo": "Cruce no permitido"}).status_code == 404

        # La referencia de sucursal/rol ajena debe fallar antes de insertar
        # usuario, perfil o auditoría en Manaco.
        with plataforma.connect() as conexion:
            manaco_id = conexion.execute(text(
                "SELECT idEmp FROM empresa WHERE codigo='manaco'")).scalar_one()
            lupo_branch, lupo_role = conexion.execute(text("""
                SELECT s.nro, r.nro FROM empresa e
                JOIN sucursal s ON s.idEmp=e.idEmp
                JOIN rol r ON r.idEmp=e.idEmp AND r.nro LIKE '%-CAJ'
                WHERE e.codigo='lupo' ORDER BY s.nro, r.nro LIMIT 1
            """)).one()
            user_count_before = conexion.execute(text(
                "SELECT count(*) FROM usuario WHERE idEmp=:idemp"), {"idemp": manaco_id}).scalar_one()
            employee_count_before = conexion.execute(text(
                "SELECT count(*) FROM empleado WHERE idEmp=:idemp"), {"idemp": manaco_id}).scalar_one()
            audit_count_before = conexion.execute(text(
                "SELECT count(*) FROM bitacora WHERE idEmp=:idemp"), {"idemp": manaco_id}).scalar_one()

        manaco_role = created_by_company["manaco"][1]
        for foreign_field, foreign_value in (("nroSuc", lupo_branch), ("nroRol", lupo_role)):
            payload = {
                "ci": f"CU05-{uuid4().hex[:12]}", "nombre": "Cruce Rechazado",
                "apellidoPat": "Tenant", "apellidoMat": "Ajeno", "sexo": "M",
                "correo": f"cu05-rejected-{uuid4().hex}@example.com",
                "telefono": "70000000", "direccion": "No persistir", "fechaNac": "1998-02-03",
                "contrasena": "Prueba-CU05-2026!", "nroRol": manaco_role,
                "cargo": "Cajero", "nroSuc": created_by_company["manaco"][2],
            }
            payload[foreign_field] = foreign_value
            response = admin.post("/api/admin/empleados", json=payload)
            assert response.status_code == 422, response.text

        with plataforma.connect() as conexion:
            assert conexion.execute(text(
                "SELECT count(*) FROM usuario WHERE idEmp=:idemp"), {"idemp": manaco_id}).scalar_one() == user_count_before
            assert conexion.execute(text(
                "SELECT count(*) FROM empleado WHERE idEmp=:idemp"), {"idemp": manaco_id}).scalar_one() == employee_count_before
            assert conexion.execute(text(
                "SELECT count(*) FROM bitacora WHERE idEmp=:idemp"), {"idemp": manaco_id}).scalar_one() == audit_count_before

        # Cajero y cliente carecen de CU05 aunque las rutas estén permitidas.
        for role in ("cajero", "cliente"):
            assert _entrar_empresa(admin, credenciales, "manaco", role).status_code == 200
            denied = admin.get("/api/admin/usuarios")
            assert denied.status_code == 403, f"{role}: {denied.text}"


@pytest.fixture
def cu06_temporales(plataforma):
    """Elimina solo empleados/roles de la prueba, deja la bitácora auditable."""
    created = {"users": [], "roles": []}
    yield created
    with plataforma.begin() as conexion:
        for idemp, correo in created["users"]:
            conexion.execute(text(
                "DELETE FROM usuario WHERE idEmp=:idemp AND lower(btrim(correo))=:correo"),
                {"idemp": idemp, "correo": correo})
        for idemp, role_id in created["roles"]:
            conexion.execute(text(
                "DELETE FROM rol_funcion WHERE idEmp=:idemp AND nroRol=:role"),
                {"idemp": idemp, "role": role_id})
            conexion.execute(text(
                "DELETE FROM rol WHERE idEmp=:idemp AND nro=:role"),
                {"idemp": idemp, "role": role_id})


def test_cu06_roles_permisos_multitenant_end_to_end(
        client, credenciales, plataforma, cu06_temporales, monkeypatch):
    """CU06 en dos tenants: CRUD, RBAC efectivo, aislamiento, RLS y rollback."""
    assert _entrar_empresa(client, credenciales, "manaco", "admin").status_code == 200
    assert client.get("/api/admin/roles").status_code == 503

    app = create_app(_configuracion("cu05,cu06,cu09,sync,saas,cu24"))
    with TestClient(app, raise_server_exceptions=False) as admin:
        admin.headers.update(CABECERAS)
        role_data: dict[str, tuple[int, str, str]] = {}
        employee_data: dict[str, tuple[int, str, str]] = {}
        catalogs = {}

        for codigo in ("manaco", "lupo"):
            company = next(row for row in credenciales["empresas"] if row["codigo"] == codigo)
            idemp = company["idEmp"]
            assert _entrar_empresa(admin, credenciales, codigo, "admin").status_code == 200
            identity = admin.get("/api/auth/me").json()
            assert identity["empresa"] == codigo and "CU06" in identity["permisos"]

            functions = admin.get("/api/admin/funciones")
            branches = admin.get("/api/admin/empleados/sucursales")
            assert functions.status_code == branches.status_code == 200
            catalogs[codigo] = {row["id"] for row in functions.json()}
            assert {"CU05", "CU06", "CU09"} <= catalogs[codigo]
            branch = branches.json()[0]

            role_id = f"R{uuid4().hex[:12]}"
            cu06_temporales["roles"].append((idemp, role_id))
            created = admin.post("/api/admin/roles", json={
                "nro": role_id, "descripcion": f"Operador {codigo}",
            })
            assert created.status_code == 201, created.text
            assert created.json()["estado"] == "activo"
            assert admin.get(f"/api/admin/roles/{role_id}").json()["descripcion"] == f"Operador {codigo}"
            assert admin.get(f"/api/admin/roles/{role_id}/permisos").json()["permisos"] == []

            edited = admin.patch(f"/api/admin/roles/{role_id}", json={
                "descripcion": f"Operador editado {codigo}",
            })
            assert edited.status_code == 200 and edited.json()["descripcion"] == f"Operador editado {codigo}"

            inactive = admin.patch(f"/api/admin/roles/{role_id}/estado-cuenta", json={"activo": False})
            assert inactive.status_code == 200 and inactive.json()["estado"] == "inactivo"
            denied_change = admin.put(f"/api/admin/roles/{role_id}/permisos", json={"permisos": ["CU05"]})
            assert denied_change.status_code == 409
            active = admin.patch(f"/api/admin/roles/{role_id}/estado-cuenta", json={"activo": True})
            assert active.status_code == 200 and active.json()["estado"] == "activo"

            # A partially valid replacement must not delete/insert permissions
            # or create audit entries when one global function is unknown.
            audit_before_invalid = plataforma.connect()
            try:
                invalid_audit_count = audit_before_invalid.execute(text("""
                    SELECT count(*) FROM bitacora WHERE idEmp=:idemp AND detalles->>'rol'=:role
                """), {"idemp": idemp, "role": role_id}).scalar_one()
            finally:
                audit_before_invalid.close()
            invalid = admin.put(f"/api/admin/roles/{role_id}/permisos", json={
                "permisos": ["CU05", "FUNCION-INEXISTENTE"],
            })
            assert invalid.status_code == 422
            assert admin.get(f"/api/admin/roles/{role_id}/permisos").json()["permisos"] == []
            with plataforma.connect() as conexion:
                assert conexion.execute(text("""
                    SELECT count(*) FROM bitacora WHERE idEmp=:idemp AND detalles->>'rol'=:role
                """), {"idemp": idemp, "role": role_id}).scalar_one() == invalid_audit_count

            # Create a user with the new role and verify permissions take effect
            # on the normal login/session path, not just in role metadata.
            email = f"cu06-{codigo}-{uuid4().hex}@example.com"
            cu06_temporales["users"].append((idemp, email))
            employee_payload = {
                "ci": f"CU06-{uuid4().hex[:12]}", "nombre": "Usuario de rol",
                "apellidoPat": "Prueba", "apellidoMat": codigo, "sexo": "M",
                "correo": email, "telefono": "70000002", "direccion": "Sucursal de prueba",
                "fechaNac": "1998-02-03", "contrasena": "Prueba-CU06-2026!",
                "nroRol": role_id, "cargo": "Operador", "nroSuc": branch["nro"],
            }
            employee = admin.post("/api/admin/empleados", json=employee_payload)
            assert employee.status_code == 201, employee.text
            employee_data[codigo] = (idemp, email, employee_payload["contrasena"])
            admin.cookies.clear()
            logged = admin.post("/api/auth/login", json={
                "empresa": codigo, "correo": email, "contrasena": employee_payload["contrasena"],
            })
            assert logged.status_code == 200, logged.text
            assert admin.get("/api/admin/usuarios").status_code == 403
            assert admin.get("/api/admin/roles").status_code == 403

            assert _entrar_empresa(admin, credenciales, codigo, "admin").status_code == 200
            grant = admin.put(f"/api/admin/roles/{role_id}/permisos", json={"permisos": ["CU05"]})
            assert grant.status_code == 200 and grant.json()["permisos"] == ["CU05"]

            # Audit failure after assignment writes must rollback both the
            # role_funcion row and audit event; no SQL grant is involved.
            if codigo == "manaco":
                from app.modules.seguridad_accesos.cu06_roles_permisos.services import rol as role_service
                original_record = role_service.record_role

                def fail_after_audit(*args, **kwargs):
                    original_record(*args, **kwargs)
                    raise RuntimeError("synthetic CU06 audit failure")

                with monkeypatch.context() as patcher:
                    patcher.setattr(role_service, "record_role", fail_after_audit)
                    failed = admin.put(f"/api/admin/roles/{role_id}/permisos", json={
                        "permisos": ["CU05", "CU09"],
                    })
                assert failed.status_code == 500
                assert admin.get(f"/api/admin/roles/{role_id}/permisos").json()["permisos"] == ["CU05"]

            admin.cookies.clear()
            logged = admin.post("/api/auth/login", json={
                "empresa": codigo, "correo": email, "contrasena": employee_payload["contrasena"],
            })
            assert logged.status_code == 200
            assert admin.get("/api/auth/me").json()["permisos"] == ["CU05"]
            assert admin.get("/api/admin/usuarios").status_code == 200
            assert admin.get("/api/admin/roles").status_code == 403

            assert _entrar_empresa(admin, credenciales, codigo, "admin").status_code == 200
            revoked = admin.put(f"/api/admin/roles/{role_id}/permisos", json={"permisos": []})
            assert revoked.status_code == 200 and revoked.json()["permisos"] == []
            admin.cookies.clear()
            assert admin.post("/api/auth/login", json={
                "empresa": codigo, "correo": email, "contrasena": employee_payload["contrasena"],
            }).status_code == 200
            assert admin.get("/api/admin/usuarios").status_code == 403
            assert _entrar_empresa(admin, credenciales, codigo, "admin").status_code == 200

            with plataforma.connect() as conexion:
                db_role = conexion.execute(text("""
                    SELECT r.idEmp, r.descripcion, r.estado FROM rol r
                    WHERE r.idEmp=:idemp AND r.nro=:role
                """), {"idemp": idemp, "role": role_id}).one()
                assert db_role[0] == idemp and db_role[1] == f"Operador editado {codigo}"
                assert db_role[2] == "activo"
                audited = conexion.execute(text("""
                    SELECT array_agg(accion ORDER BY id) FROM bitacora
                    WHERE idEmp=:idemp AND usuario_id=:actor AND detalles->>'rol'=:role
                """), {"idemp": idemp, "actor": identity["usuario"]["idUsuario"],
                      "role": role_id}).scalar_one()
                assert {"rol_creado", "rol_actualizado", "rol_desactivado", "rol_activado",
                        "permisos_rol_actualizados"} <= set(audited or [])

            role_data[codigo] = (idemp, role_id, email)
            assert role_id in {row["nro"] for row in admin.get("/api/admin/roles").json()["items"]}

            # Deactivation is rejected once employees use the role.
            occupied = admin.patch(f"/api/admin/roles/{role_id}/estado-cuenta", json={"activo": False})
            assert occupied.status_code == 409

        # Global PK on rol.nro is part of the installed schema: a foreign
        # identifier collision is a generic conflict, not a cross-tenant read.
        assert _entrar_empresa(admin, credenciales, "manaco", "admin").status_code == 200
        lupo_role = role_data["lupo"][1]
        manaco_role = role_data["manaco"][1]
        manaco_role_ids = {row["nro"] for row in admin.get("/api/admin/roles").json()["items"]}
        lupo_role_ids = {row["nro"] for row in admin.get("/api/admin/roles").json()["items"]}
        assert lupo_role not in manaco_role_ids
        assert admin.get(f"/api/admin/roles/{lupo_role}").status_code == 404
        assert admin.patch(f"/api/admin/roles/{lupo_role}", json={"descripcion": "Cruce"}).status_code == 404
        assert admin.put(f"/api/admin/roles/{lupo_role}/permisos", json={"permisos": []}).status_code == 404
        before_collision = len(manaco_role_ids)
        collision = admin.post("/api/admin/roles", json={"nro": lupo_role, "descripcion": "Código ajeno"})
        assert collision.status_code == 409
        assert len({row["nro"] for row in admin.get("/api/admin/roles").json()["items"]}) == before_collision
        assert manaco_role in manaco_role_ids

        # A client and a cashier cannot use CU06 despite allowlisting.
        for role in ("cajero", "cliente"):
            assert _entrar_empresa(admin, credenciales, "manaco", role).status_code == 200
            assert admin.get("/api/admin/roles").status_code == 403
            assert admin.get("/api/admin/funciones").status_code == 403


def test_matriz_roles_y_permisos_en_cinco_empresas(client, credenciales):
    esperados = {
        "admin": {"CU05", "CU06", "CU07", "CU08", "CU09", "CU15", "CU18", "CU19", "CU20", "CU21", "CU22", "CU23", "CU24", "CU25"},
        "encargado": {"CU11", "CU15", "CU20", "CU22", "CU23"},
        "cajero": {"CU24"},
        "cliente": {"CU10", "CU11", "CU12", "CU13", "CU14", "CU15", "CU17"},
    }
    for codigo in EMPRESAS:
        for rol, permisos in esperados.items():
            respuesta = _entrar_empresa(client, credenciales, codigo, rol)
            assert respuesta.status_code == 200, f"{codigo}/{rol}: {respuesta.text}"
            identidad = client.get("/api/auth/me")
            assert identidad.status_code == 200
            assert identidad.json()["empresa"] == codigo
            assert set(identidad.json()["permisos"]) == permisos, f"{codigo}/{rol} permisos distintos"


def test_plataforma_exige_sesion_de_plataforma(client, credenciales, empresa_por_codigo):
    # Token de empresa en una ruta de plataforma: rechazado.
    assert _entrar_empresa(client, credenciales, "manaco", "admin").status_code == 200
    assert client.get("/api/saas/empresas").status_code == 403
    assert client.get("/api/saas/planes").status_code == 403
    # Token de plataforma en una ruta de empresa: rechazado.
    assert _entrar_plataforma(client, credenciales).status_code == 200
    assert client.get("/api/admin/sucursales").status_code == 401
    assert client.get("/api/sync/operaciones").status_code == 401


def test_credenciales_demo_solo_con_permiso(client, credenciales, plataforma):
    # Temporalmente sin el permiso SAAS-DEMO: 403, no 404 ni fuga de datos.
    with plataforma.begin() as conexion:
        conexion.execute(text(
            "DELETE FROM rol_funcion WHERE nroRol = 'SAAS-ADMIN' AND idFun = 'SAAS-DEMO'"))
    try:
        assert _entrar_plataforma(client, credenciales).status_code == 200
        assert client.get("/api/saas/demo/credenciales").status_code == 403
    finally:
        with plataforma.begin() as conexion:
            conexion.execute(text("""
                INSERT INTO rol_funcion(nroRol, idFun, descripcion, idEmp)
                VALUES ('SAAS-ADMIN', 'SAAS-DEMO', 'Acceso a credenciales de demostracion', NULL)
                ON CONFLICT (nroRol, idFun) DO NOTHING
            """))


# ---------------------------------------------------------------------------
# Login por empresa (mismo correo en cinco tenants)
# ---------------------------------------------------------------------------

def test_mismo_correo_en_cinco_empresas(client, credenciales):
    identificadores = set()
    for codigo in EMPRESAS:
        respuesta = _entrar_empresa(client, credenciales, codigo, "cajero")
        assert respuesta.status_code == 200, f"{codigo}: {respuesta.text}"
        cuerpo = respuesta.json()
        assert cuerpo["empresa"] == codigo
        assert cuerpo["plataforma"] is False
        assert "CU24" in cuerpo["permisos"]
        identificadores.add(cuerpo["usuario"]["idUsuario"])
    assert len(identificadores) == len(EMPRESAS)


def test_clave_de_otra_empresa_no_autentica(client, credenciales):
    kosy = next(e for e in credenciales["empresas"] if e["codigo"] == "kosy-pantalones")
    client.cookies.clear()
    respuesta = client.post("/api/auth/login", json={
        "empresa": "manaco",
        "correo": kosy["usuarios"]["cajero"]["correo"],
        "contrasena": kosy["usuarios"]["cajero"]["contrasena"],
    })
    assert respuesta.status_code == 401
    # Sin código de empresa no hay resolución de tenant.
    assert client.post("/api/auth/login", json={
        "correo": kosy["usuarios"]["cajero"]["correo"],
        "contrasena": kosy["usuarios"]["cajero"]["contrasena"],
    }).status_code == 401


def test_login_de_empresa_rechaza_cuenta_de_plataforma(client, credenciales):
    client.cookies.clear()
    respuesta = client.post("/api/auth/login", json={
        "empresa": "manaco",
        "correo": credenciales["plataforma"]["correo"],
        "contrasena": credenciales["plataforma"]["contrasena"],
    })
    assert respuesta.status_code == 401


def test_me_y_logout_de_empresa(client, credenciales):
    assert _entrar_empresa(client, credenciales, "lupo", "cliente").status_code == 200
    identidad = client.get("/api/auth/me")
    assert identidad.status_code == 200
    assert identidad.json()["empresa"] == "lupo"
    assert client.post("/api/auth/logout").status_code == 204
    assert client.get("/api/auth/me").status_code == 401


def test_me_y_logout_de_plataforma(client, credenciales):
    assert _entrar_plataforma(client, credenciales).status_code == 200
    identidad = client.get("/api/auth/me")
    assert identidad.status_code == 200
    assert identidad.json()["plataforma"] is True
    assert client.post("/api/auth/logout").status_code == 204
    assert client.get("/api/auth/me").status_code == 401


# ---------------------------------------------------------------------------
# Aislamiento por empresa sobre rutas HTTP y RLS directa
# ---------------------------------------------------------------------------

def test_rutas_de_empresa_solo_devuelven_su_tenant(client, credenciales, plataforma):
    with plataforma.connect() as conexion:
        filas = conexion.execute(text("""
            SELECT e.codigo, s.nombre
            FROM empresa e JOIN sucursal s ON s.idEmp = e.idEmp
        """)).all()
    esperado: dict[str, set[str]] = {}
    for codigo, nombre in filas:
        esperado.setdefault(codigo, set()).add(nombre)

    for codigo in EMPRESAS:
        assert _entrar_empresa(client, credenciales, codigo, "admin").status_code == 200
        respuesta = client.get("/api/admin/sucursales")
        assert respuesta.status_code == 200, respuesta.text
        cuerpo = respuesta.json()
        nombres = {sucursal["nombre"] for sucursal in cuerpo["items"]}
        assert nombres == esperado[codigo], codigo
        assert cuerpo["total"] == len(esperado[codigo])


def test_rls_directa_lectura_y_escritura_cruzada(plataforma, datos):
    a, b = datos("manaco"), datos("kosy-pantalones")
    motor = create_engine(APP_URL)
    try:
        with motor.connect() as conexion:
            conexion.execute(text("SELECT set_config('app.idemp', :idemp, true)"),
                             {"idemp": str(a["idEmp"])})
            assert conexion.execute(text("SELECT count(*) FROM producto WHERE idEmp = :idemp"),
                                    {"idemp": a["idEmp"]}).scalar_one() > 0
            assert conexion.execute(text("SELECT count(*) FROM producto WHERE idEmp = :idemp"),
                                    {"idemp": b["idEmp"]}).scalar_one() == 0
            actualizadas = conexion.execute(text(
                "UPDATE producto SET descripcion = descripcion WHERE idEmp = :idemp"),
                {"idemp": b["idEmp"]}).rowcount
            assert actualizadas == 0
            with pytest.raises(DBAPIError):
                conexion.execute(text("""
                    INSERT INTO producto(idprod, descripcion, estado, idcat, idmarca,
                                         idcol, idprov, idemp)
                    VALUES ('PYTEST-X', 'cruzado', 'activo',
                            (SELECT min(idcat) FROM categoria),
                            (SELECT min(idmarca) FROM marca),
                            (SELECT min(idcol) FROM coleccion),
                            (SELECT min(idprov) FROM proveedor), :idemp)
                """), {"idemp": b["idEmp"]})
            conexion.rollback()
        # Sin contexto de tenant no se lee ninguna fila.
        with motor.connect() as conexion:
            assert conexion.execute(text("SELECT count(*) FROM producto")).scalar_one() == 0
    finally:
        motor.dispose()


# ---------------------------------------------------------------------------
# Motor de sincronización
# ---------------------------------------------------------------------------

def test_venta_offline_idempotente(client, credenciales, plataforma, datos):
    manaco = datos("manaco")
    assert _entrar_empresa(client, credenciales, "manaco", "cajero").status_code == 200
    operacion = uuid4()
    lote = _lote(operacion, manaco["sucursal"], manaco["idVariante"], manaco["precio"])
    stock_antes = _stock(plataforma, manaco["idEmp"], manaco["sucursal"], manaco["idVariante"])

    primero = client.post("/api/sync/lote", json=lote)
    assert primero.status_code == 200, primero.text
    resultado = primero.json()["resultados"][0]
    assert resultado["estado"] == "sincronizada"
    assert resultado["nroVenta"]
    assert primero.json()["sincronizadas"] == 1
    assert _ventas(plataforma, manaco["idEmp"], operacion) == 1
    assert _stock(plataforma, manaco["idEmp"], manaco["sucursal"], manaco["idVariante"]) == stock_antes - 1
    assert _efectos_venta(plataforma, manaco["idEmp"], operacion) == {
        "ventas": 1, "pagos": 1, "detalles": 1, "movimientos": 1,
    }
    repetido = client.post("/api/sync/lote", json=lote)
    assert repetido.status_code == 200
    segundo = repetido.json()["resultados"][0]
    assert segundo["estado"] == "sincronizada"
    assert segundo["idempotente"] is True
    assert segundo["nroVenta"] == resultado["nroVenta"]
    assert _ventas(plataforma, manaco["idEmp"], operacion) == 1
    assert _stock(plataforma, manaco["idEmp"], manaco["sucursal"], manaco["idVariante"]) == stock_antes - 1


def test_pago_no_efectivo_no_se_confirma_sin_pasarela(client, credenciales, plataforma, datos):
    manaco = datos("manaco")
    assert _entrar_empresa(client, credenciales, "manaco", "cajero").status_code == 200
    operacion = uuid4()
    lote = _lote(operacion, manaco["sucursal"], manaco["idVariante"], manaco["precio"], metodo="tarjeta")
    resultado = client.post("/api/sync/lote", json=lote).json()["resultados"][0]
    assert resultado["estado"] == "sincronizada"
    with plataforma.connect() as conexion:
        estado = conexion.execute(text("""
            SELECT p.estado FROM pago p JOIN venta v
              ON v.idEmp=p.idEmp AND v.nroVenta=p.nroVenta
            WHERE v.idEmp=:idemp AND v.idOperacion=CAST(:uuid AS uuid)
        """), {"idemp": manaco["idEmp"], "uuid": str(operacion)}).scalar_one()
    assert estado == "pendiente"


def test_respuesta_perdida_despues_del_commit_se_recupera_sin_duplicar(credenciales, plataforma, datos):
    manaco = datos("manaco")
    app = create_app(_configuracion())

    @app.middleware("http")
    async def perder_respuesta_despues_del_commit(request, call_next):
        response = await call_next(request)
        if (request.url.path == "/api/sync/lote"
                and request.headers.get("x-test-drop-after-commit") == "1"):
            raise RuntimeError("respuesta de prueba descartada")
        return response

    with TestClient(app, raise_server_exceptions=False) as caja:
        caja.headers.update(CABECERAS)
        assert _entrar_empresa(caja, credenciales, "manaco", "cajero").status_code == 200
        operacion = uuid4()
        lote = _lote(operacion, manaco["sucursal"], manaco["idVariante"], manaco["precio"])
        stock_antes = _stock(plataforma, manaco["idEmp"], manaco["sucursal"], manaco["idVariante"])

        perdida = caja.post("/api/sync/lote", json=lote, headers={"X-Test-Drop-After-Commit": "1"})
        assert perdida.status_code >= 500
        assert _ventas(plataforma, manaco["idEmp"], operacion) == 1

        recuperada = caja.post("/api/sync/lote", json=lote).json()["resultados"][0]
        assert recuperada["estado"] == "sincronizada"
        assert recuperada["idempotente"] is True
        assert recuperada["nroVenta"]
        assert _ventas(plataforma, manaco["idEmp"], operacion) == 1
        assert _stock(plataforma, manaco["idEmp"], manaco["sucursal"], manaco["idVariante"]) == stock_antes - 1
        assert _efectos_venta(plataforma, manaco["idEmp"], operacion) == {
            "ventas": 1, "pagos": 1, "detalles": 1, "movimientos": 1,
        }


@pytest.fixture
def inventario_concurrente_lupo(plataforma, datos):
    """Obtiene el stock del escenario concurrente; su fixture común restaura ventas."""
    lupo = datos("lupo")
    with plataforma.begin() as conexion:
        fila = conexion.execute(text("""
            SELECT nroInv AS "nroInv", stock, cantDisp AS "cantDisp" FROM inventario
            WHERE idEmp=:idemp AND nroSuc=:sucursal AND idVar=:variante
            FOR UPDATE
        """), {
            "idemp": lupo["idEmp"], "sucursal": lupo["sucursal"],
            "variante": lupo["idVariante"],
        }).mappings().one()
        assert fila["cantDisp"] > 0, "Fixture Lupo sin stock disponible al iniciar el test"
        inicial = dict(fila)

    operaciones = (uuid4(), uuid4())
    yield lupo, inicial, operaciones


def test_ventas_concurrentes_no_venden_mas_stock_que_el_disponible(
        credenciales, plataforma, inventario_concurrente_lupo):
    """Dos cajas compiten por todo el stock; el test restaura sus propios efectos."""
    # Se usa Lupo para no consumir la combinación de Manaco compartida por las
    # demás pruebas del módulo.
    lupo, inicial, operaciones = inventario_concurrente_lupo
    stock = inicial["cantDisp"]
    assert stock > 0
    operaciones = list(operaciones)
    lotes = [_lote(op_id, lupo["sucursal"], lupo["idVariante"], lupo["precio"], cantidad=stock)
             for op_id in operaciones]
    barrera = threading.Barrier(2)

    with ExitStack() as stack:
        caja_a = stack.enter_context(TestClient(create_app(_configuracion()), raise_server_exceptions=False))
        caja_b = stack.enter_context(TestClient(create_app(_configuracion()), raise_server_exceptions=False))
        cajas = (caja_a, caja_b)
        for caja in cajas:
            caja.headers.update(CABECERAS)
            login = _entrar_empresa(caja, credenciales, "lupo", "cajero")
            assert login.status_code == 200, login.text

        def enviar(indice):
            barrera.wait(timeout=10)
            response = cajas[indice].post("/api/sync/lote", json=lotes[indice])
            assert response.status_code == 200, response.text
            return response.json()["resultados"][0]

        with ThreadPoolExecutor(max_workers=2) as pool:
            resultados = list(pool.map(enviar, (0, 1)))

    assert sorted(resultado["estado"] for resultado in resultados) == ["conflicto", "sincronizada"]
    rechazada = next(resultado for resultado in resultados if resultado["estado"] == "conflicto")
    assert rechazada["codigo"] == "stock_insuficiente"
    assert _stock(plataforma, lupo["idEmp"], lupo["sucursal"], lupo["idVariante"]) == 0
    id_aceptada = next(op_id for op_id, resultado in zip(operaciones, resultados)
                       if resultado["estado"] == "sincronizada")
    id_rechazada = next(op_id for op_id, resultado in zip(operaciones, resultados)
                        if resultado["estado"] == "conflicto")
    assert _efectos_venta(plataforma, lupo["idEmp"], id_aceptada) == {
        "ventas": 1, "pagos": 1, "detalles": 1, "movimientos": 1,
    }
    assert _efectos_venta(plataforma, lupo["idEmp"], id_rechazada) == {
        "ventas": 0, "pagos": 0, "detalles": 0, "movimientos": 0,
    }

def test_mismo_uuid_con_otro_contenido_es_conflicto(client, credenciales, plataforma, datos):
    manaco = datos("manaco")
    assert _entrar_empresa(client, credenciales, "manaco", "cajero").status_code == 200
    operacion = uuid4()
    original = _lote(operacion, manaco["sucursal"], manaco["idVariante"], manaco["precio"])
    assert client.post("/api/sync/lote", json=original).json()["resultados"][0]["estado"] == "sincronizada"

    alterado = _lote(operacion, manaco["sucursal"], manaco["idVariante"], manaco["precio"], cantidad=2)
    respuesta = client.post("/api/sync/lote", json=alterado)
    resultado = respuesta.json()["resultados"][0]
    assert resultado["estado"] == "conflicto"
    assert resultado["codigo"] == "contenido_incompatible"
    assert _ventas(plataforma, manaco["idEmp"], operacion) == 1


def test_lote_parcial_y_conflicto_de_stock(client, credenciales, plataforma, datos):
    manaco = datos("manaco")
    assert _entrar_empresa(client, credenciales, "manaco", "cajero").status_code == 200
    valida, invalida = uuid4(), uuid4()
    lote = _lote(valida, manaco["sucursal"], manaco["idVariante"], manaco["precio"])
    lote["operaciones"].append(
        _lote(invalida, manaco["sucursal"], "NO-EXISTE", manaco["precio"])["operaciones"][0])

    respuesta = client.post("/api/sync/lote", json=lote)
    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    por_id = {resultado["id"]: resultado for resultado in cuerpo["resultados"]}
    assert por_id[str(valida)]["estado"] == "sincronizada"
    assert por_id[str(invalida)]["estado"] == "conflicto"
    assert por_id[str(invalida)]["codigo"] == "stock_insuficiente"
    assert cuerpo["sincronizadas"] == 1 and cuerpo["conflictos"] == 1
    assert _ventas(plataforma, manaco["idEmp"], valida) == 1
    assert _ventas(plataforma, manaco["idEmp"], invalida) == 0


@pytest.fixture(scope="module")
def conflicto_registrado(client, credenciales, datos, plataforma):
    manaco = datos("manaco")
    assert _entrar_empresa(client, credenciales, "manaco", "cajero").status_code == 200
    operacion = uuid4()
    stock = _stock(plataforma, manaco["idEmp"], manaco["sucursal"], manaco["idVariante"])
    lote = _lote(operacion, manaco["sucursal"], manaco["idVariante"], manaco["precio"],
                 cantidad=max(stock + 1, 2))
    resultado = client.post("/api/sync/lote", json=lote).json()["resultados"][0]
    assert resultado["estado"] == "conflicto"
    assert resultado["codigo"] == "stock_insuficiente"
    yield operacion
    with plataforma.begin() as conexion:
        conexion.execute(text("""
            DELETE FROM bitacora
            WHERE idEmp=:idemp AND detalles->>'idOperacion'=:operacion
        """), {"idemp": manaco["idEmp"], "operacion": str(operacion)})
        conexion.execute(text("""
            DELETE FROM operacion_sync WHERE idEmp=:idemp AND idOperacion=:operacion
        """), {"idemp": manaco["idEmp"], "operacion": operacion})


def test_dependencias(client, credenciales, inventario_dependencias):
    kosy = inventario_dependencias
    assert _entrar_empresa(client, credenciales, "kosy-pantalones", "cajero").status_code == 200

    # La fixture inserta inventario propio y lo elimina después del cleanup de
    # las ventas, sin alterar ni depender de las existencias compartidas.
    rechazada = uuid4()
    respuesta = client.post("/api/sync/lote", json=_lote(
        rechazada, kosy["sucursal"], kosy["idVariante"], kosy["precio"],
        cantidad=kosy["stock"] + 1))
    resultado = respuesta.json()["resultados"][0]
    assert resultado["estado"] == "conflicto"
    assert resultado["codigo"] == "stock_insuficiente"

    # Dependencia de una operación rechazada: conflicto terminal.
    respuesta = client.post("/api/sync/lote", json=_lote(
        uuid4(), kosy["sucursal"], kosy["idVariante"], kosy["precio"],
        depende=[rechazada]))
    resultado = respuesta.json()["resultados"][0]
    assert resultado["estado"] == "conflicto"
    assert resultado["codigo"] == "dependencia_rechazada"

    # Dependencia aún no sincronizada: no se confirma ni se pierde.
    respuesta = client.post("/api/sync/lote", json=_lote(
        uuid4(), kosy["sucursal"], kosy["idVariante"], kosy["precio"],
        depende=[uuid4()]))
    resultado = respuesta.json()["resultados"][0]
    assert resultado["estado"] == "conflicto"
    assert resultado["codigo"] == "dependencia_pendiente"

    # Dependencia dentro del mismo lote: se procesa en orden.
    primera, segunda = uuid4(), uuid4()
    lote = _lote(primera, kosy["sucursal"], kosy["idVariante"], kosy["precio"])
    lote["operaciones"].append(
        _lote(segunda, kosy["sucursal"], kosy["idVariante"], kosy["precio"],
              depende=[primera])["operaciones"][0])
    cuerpo = client.post("/api/sync/lote", json=lote).json()
    assert cuerpo["sincronizadas"] == 2, cuerpo


def test_recuperacion_de_operacion_colgada(client, credenciales, plataforma, datos):
    manaco = datos("manaco")
    assert _entrar_empresa(client, credenciales, "manaco", "cajero").status_code == 200
    operacion = uuid4()
    lote = _lote(operacion, manaco["sucursal"], manaco["idVariante"], manaco["precio"])
    primero = client.post("/api/sync/lote", json=lote).json()["resultados"][0]
    assert primero["estado"] == "sincronizada"

    # Simula un cierre inesperado: la operación quedó tomada hace > 5 minutos.
    with plataforma.begin() as conexion:
        conexion.execute(text("""
            UPDATE operacion_sync
               SET estado = 'en_proceso',
                   fechaRecepcion = now() - interval '10 minutes'
             WHERE idEmp = :idemp AND idOperacion = CAST(:uuid AS uuid)
        """), {"idemp": manaco["idEmp"], "uuid": str(operacion)})

    recuperada = client.post("/api/sync/lote", json=lote).json()["resultados"][0]
    assert recuperada["estado"] == "sincronizada", recuperada
    assert recuperada["nroVenta"] == primero["nroVenta"]
    assert _ventas(plataforma, manaco["idEmp"], operacion) == 1


def test_permisos_por_rol_en_sincronizacion(client, credenciales, datos, conflicto_registrado):
    manaco = datos("manaco")
    # El cliente no puede registrar ventas presenciales (CU24).
    assert _entrar_empresa(client, credenciales, "manaco", "cliente").status_code == 200
    resultado = client.post("/api/sync/lote", json=_lote(
        uuid4(), manaco["sucursal"], manaco["idVariante"], manaco["precio"])).json()["resultados"][0]
    assert resultado["estado"] == "rechazada"
    assert resultado["codigo"] == "permiso_denegado"

    # Resolver conflictos requiere CU22/CU24.
    resolucion = client.post(f"/api/sync/conflictos/{conflicto_registrado}/resolver",
                             json={"detalle": "Revisado con el cliente"})
    assert resolucion.status_code == 403

    # El administrador sí puede resolverlo, y queda auditado en la operación.
    assert _entrar_empresa(client, credenciales, "manaco", "admin").status_code == 200
    resolucion = client.post(f"/api/sync/conflictos/{conflicto_registrado}/resolver",
                             json={"detalle": "Revisado con el administrador"})
    assert resolucion.status_code == 200, resolucion.text
    assert resolucion.json()["estado"] == "rechazada"


def test_historial_solo_del_tenant(client, credenciales, datos):
    manaco = datos("manaco")
    assert _entrar_empresa(client, credenciales, "manaco", "cajero").status_code == 200
    operacion = uuid4()
    resultado = client.post("/api/sync/lote", json=_lote(
        operacion, manaco["sucursal"], manaco["idVariante"], manaco["precio"])).json()["resultados"][0]
    assert resultado["estado"] == "sincronizada"

    assert _entrar_empresa(client, credenciales, "urbanwomen", "admin").status_code == 200
    historial = client.get("/api/sync/operaciones")
    assert historial.status_code == 200
    assert str(operacion) not in historial.text

    assert _entrar_empresa(client, credenciales, "manaco", "admin").status_code == 200
    assert str(operacion) in client.get("/api/sync/operaciones").text


def test_idempotencia_concurrente_en_postgresql(plataforma, datos):
    """Dos conexiones compiten por el mismo UUID: solo una crea la operación."""
    manaco = datos("manaco")
    operacion = str(uuid4())
    url = APP_URL.replace("postgresql+psycopg://", "postgresql://", 1)
    barrera = threading.Barrier(2)
    resultados: dict[int, str] = {}

    def competir(indice: int) -> None:
        with psycopg.connect(url) as conexion:
            conexion.execute("SELECT set_config('app.idemp', %s, true)", (str(manaco["idEmp"]),))
            barrera.wait(timeout=10)
            estado = conexion.execute("""
                    SELECT fn_sync_tomar_operacion(
                      CAST(%s AS integer), CAST(%s AS uuid), CAST('venta' AS varchar),
                      CAST(%s AS integer), CAST(%s AS varchar), CAST(now() AS timestamptz),
                      CAST('{"lineas": []}' AS jsonb), CAST(repeat('a', 64) AS char(64)),
                      CAST('pytest' AS varchar), CAST(1 AS smallint))
                """, (manaco["idEmp"], operacion, manaco["sucursal"], manaco["cajero"])).fetchone()[0]
            resultados[indice] = estado
            # Solo la conexión que creó la operación confirma; la otra libera el
            # bloqueo sin persistir nada. La fila se elimina al final de la prueba.
            if estado == "nueva":
                conexion.commit()
            else:
                conexion.rollback()

    hilos = [threading.Thread(target=competir, args=(i,)) for i in (0, 1)]
    for hilo in hilos:
        hilo.start()
    for hilo in hilos:
        hilo.join(timeout=30)

    assert sorted(resultados.values()) == ["en_proceso", "nueva"], resultados
    with plataforma.connect() as conexion:
        filas = conexion.execute(text("""
            SELECT count(*) FROM operacion_sync
            WHERE idEmp = :idemp AND idOperacion = CAST(:uuid AS uuid)
        """), {"idemp": manaco["idEmp"], "uuid": operacion}).scalar_one()
    assert filas == 1
    # Limpieza explícita de la fila de prueba (rol de plataforma).
    with plataforma.begin() as conexion:
        conexion.execute(text("""
            DELETE FROM operacion_sync
            WHERE idEmp = :idemp AND idOperacion = CAST(:uuid AS uuid)
        """), {"idemp": manaco["idEmp"], "uuid": operacion})
