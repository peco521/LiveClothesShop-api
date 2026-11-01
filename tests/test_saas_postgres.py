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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from app.core.config import Settings
from app.main import create_app

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


def _validar_url_test(url: str | None, variable: str) -> None:
    if url is None:
        return
    parsed = make_url(url)
    if (parsed.host != "127.0.0.1" or parsed.port != 55432
            or parsed.database != "fashionstore_test"):
        raise RuntimeError(
            f"{variable} solo puede apuntar a 127.0.0.1:55432/fashionstore_test")


_validar_url_test(APP_URL, "TEST_DATABASE_URL")
_validar_url_test(PLATFORM_URL, "TEST_PLATFORM_DATABASE_URL")


def _base_disponible() -> bool:
    if not (APP_URL and PLATFORM_URL and CREDENCIALES.exists()):
        return False
    try:
        motor = create_engine(PLATFORM_URL)
        with motor.connect() as conexion:
            conexion.execute(text("select 1 from empresa limit 1"))
        motor.dispose()
        return True
    except Exception:
        return False


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


@pytest.fixture(scope="module")
def plataforma():
    motor = create_engine(PLATFORM_URL)
    yield motor
    motor.dispose()


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


def test_panel_saas_planes_identidad_suscripcion_soporte_y_auditoria(client, credenciales):
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
    assert client.get("/api/saas/empresas").status_code == 401
    assert client.get("/api/saas/planes").status_code == 401
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
    """Reserva una combinación y limpia exclusivamente los efectos de su test."""
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
    try:
        yield lupo, inicial, operaciones
    finally:
        # Sync puede registrar también la operación rechazada. Se borran solo
        # las dos UUID de esta ejecución, nunca datos preexistentes del tenant.
        with plataforma.begin() as conexion:
            ventas = conexion.execute(text("""
                SELECT nroVenta FROM venta
                WHERE idEmp=:idemp AND idOperacion = ANY(:operaciones)
            """), {"idemp": lupo["idEmp"],
                  "operaciones": list(operaciones)}).scalars().all()
            conexion.execute(text("""
                DELETE FROM operacion_sync
                WHERE idEmp=:idemp AND idOperacion = ANY(:operaciones)
            """), {"idemp": lupo["idEmp"], "operaciones": list(operaciones)})
            if ventas:
                conexion.execute(text("""
                    DELETE FROM pago WHERE idEmp=:idemp AND nroVenta = ANY(:ventas)
                """), {"idemp": lupo["idEmp"], "ventas": ventas})
                conexion.execute(text("""
                    DELETE FROM bitacora
                    WHERE idEmp=:idemp AND detalles->>'idOperacion' = ANY(:operaciones_texto)
                """), {"idemp": lupo["idEmp"],
                      "operaciones_texto": [str(op) for op in operaciones]})
                conexion.execute(text("""
                    DELETE FROM movimiento_inv
                    WHERE idEmp=:idemp AND motivo LIKE ANY(:motivos)
                """), {"idemp": lupo["idEmp"],
                      "motivos": [f"% #{venta}%" for venta in ventas]})
                conexion.execute(text("""
                    DELETE FROM venta WHERE idEmp=:idemp AND idOperacion = ANY(:operaciones)
                """), {"idemp": lupo["idEmp"], "operaciones": list(operaciones)})
            conexion.execute(text("""
                UPDATE inventario SET stock=:stock, cantDisp=:disponible
                WHERE nroInv=:nroinv AND idEmp=:idemp
            """), {"stock": inicial["stock"], "disponible": inicial["cantDisp"],
                  "nroinv": inicial["nroInv"], "idemp": lupo["idEmp"]})


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
    return operacion


def test_dependencias(client, credenciales, datos, conflicto_registrado):
    manaco = datos("manaco")
    assert _entrar_empresa(client, credenciales, "manaco", "cajero").status_code == 200

    # Dependencia de una operación rechazada: conflicto terminal.
    respuesta = client.post("/api/sync/lote", json=_lote(
        uuid4(), manaco["sucursal"], manaco["idVariante"], manaco["precio"],
        depende=[conflicto_registrado]))
    resultado = respuesta.json()["resultados"][0]
    assert resultado["estado"] == "conflicto"
    assert resultado["codigo"] == "dependencia_rechazada"

    # Dependencia aún no sincronizada: no se confirma ni se pierde.
    respuesta = client.post("/api/sync/lote", json=_lote(
        uuid4(), manaco["sucursal"], manaco["idVariante"], manaco["precio"],
        depende=[uuid4()]))
    resultado = respuesta.json()["resultados"][0]
    assert resultado["estado"] == "conflicto"
    assert resultado["codigo"] == "dependencia_pendiente"

    # Dependencia dentro del mismo lote: se procesa en orden.
    primera, segunda = uuid4(), uuid4()
    lote = _lote(primera, manaco["sucursal"], manaco["idVariante"], manaco["precio"])
    lote["operaciones"].append(
        _lote(segunda, manaco["sucursal"], manaco["idVariante"], manaco["precio"],
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
    assert _entrar_empresa(client, credenciales, "urbanwomen", "admin").status_code == 200
    historial = client.get("/api/sync/operaciones")
    assert historial.status_code == 200
    assert historial.json()["total"] == 0  # urbanwomen no sincronizó nada en estas pruebas
    # La empresa manaco sí tiene operaciones, pero no son visibles aquí.
    assert _entrar_empresa(client, credenciales, "manaco", "admin").status_code == 200
    assert client.get("/api/sync/operaciones").json()["total"] > 0


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
