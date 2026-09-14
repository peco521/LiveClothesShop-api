"""CU14 Pago electrónico: HTTP/domain regression con SQLite inyectado."""
from datetime import date

import pytest
from sqlalchemy import func, select

from app.integrations.payments.mock import PasarelaMock
from app.modules.cliente_experiencia_compra.shared.models.catalogo import (
    Categoria,
    Coleccion,
    Inventario,
    Marca,
    Producto,
    Promocion,
    Proveedor,
    Talla,
    VarianteProd,
)
from app.modules.cliente_experiencia_compra.shared.models.comercio import (
    Carrito,
    DetalleVenta,
    MovimientoInv,
    Pago,
    Venta,
)
from app.modules.cliente_experiencia_compra.cu14_pago_electronico.services import pago as service
from app.modules.cliente_experiencia_compra.cu14_pago_electronico.schemas.pago import PagoCrear
from app.modules.seguridad_accesos.models import Bitacora
from app.modules.seguridad_accesos.shared.models import Ciudad, Sucursal
from app.core.errors import DomainError

BASE = "/api/cliente/pagos"
CARRITO = "/api/cliente/carrito/items"
COMPRAS = "/api/cliente/compras/desde-carrito"


@pytest.fixture
def catalogo(factory):
    with factory.begin() as db:
        for row in (Categoria(idcat=1, descripcion="Camisas"),
                    Marca(idmarca=1, nombre="Andes", estado="activo"),
                    Coleccion(idcol=1, descripcion="Otoño"),
                    Proveedor(idprov=1, nombre="Prov", correo="p@example.com",
                              direccion="Dir", telefono="70000000"),
                    Promocion(idpromo=1, nombre="Promo10", descripcion="D",
                              tipodescuento="porcentaje", valordescuento=10,
                              fechaini=date(2026, 1, 1), fechafin=date(2026, 12, 31),
                              estado="activo"),
                    Talla(idtalla=1, descripcion="M"),
                    Ciudad(id=1, nombre="La Paz")):
            db.add(row)
        db.flush()
        for row in (Producto(idprod="p1", descripcion="Camisa", estado="activo",
                             idpromo=1, idcat=1, idmarca=1, idcol=1, idprov=1),
                    Sucursal(nro=1, nombre="Central", direccion="Dir 1", estado="activo", idciud=1),
                    Sucursal(nro=2, nombre="Sur", direccion="Dir 2", estado="activo", idciud=1)):
            db.add(row)
        db.flush()
        for row in (VarianteProd(idvariante="v1", sku="SKU-1", precio=100,
                                 estado="activo", img=None, idtalla=1, idprod="p1"),
                    VarianteProd(idvariante="v2", sku="SKU-2", precio=150,
                                 estado="activo", img=None, idtalla=1, idprod="p1"),
                    VarianteProd(idvariante="v3", sku="SKU-3", precio=50,
                                 estado="activo", img=None, idtalla=1, idprod="p1")):
            db.add(row)
        db.flush()
        for row in (Inventario(nroinv=1, stock=10, cantdisp=4, nrosuc=1, idvar="v1"),
                    Inventario(nroinv=2, stock=5, cantdisp=5, nrosuc=1, idvar="v2"),
                    Inventario(nroinv=3, stock=1, cantdisp=1, nrosuc=1, idvar="v3"),
                    Inventario(nroinv=4, stock=5, cantdisp=2, nrosuc=1, idvar="v3")):
            db.add(row)
        db.flush()


@pytest.fixture
def customer(client, registered, credentials, catalogo):
    assert client.post("/api/auth/login", json=credentials).status_code == 200
    return registered["idUsuario"]


def comprar(client, items, suc=1, **extra):
    for item in items:
        assert client.post(CARRITO, json=item).status_code == 201
    response = client.post(COMPRAS, json={"nroSuc": suc, **extra})
    assert response.status_code == 201
    return response.json()


def inventario(factory):
    with factory() as db:
        return [(r.nroinv, r.stock, r.cantdisp) for r in
                db.scalars(select(Inventario).order_by(Inventario.nroinv))]


def movimientos(factory):
    with factory() as db:
        return [(m.nroinv, m.tipomov, m.cantidad, m.motivo) for m in
                db.scalars(select(MovimientoInv).order_by(MovimientoInv.idmov))]


def bearer(client, settings):
    token = client.cookies.get(settings.cookie_name)
    assert token and len(token) == 43
    client.cookies.clear()
    return {"Authorization": f"Bearer {token}"}


def test_pagar_venta_propia(client, customer):
    venta = comprar(client, [{"idVar": "v1", "cantidad": 1}])
    response = client.post(BASE, json={"nroVenta": venta["nroVenta"], "metodo": "tarjeta"})
    assert response.status_code == 201
    data = response.json()
    assert data["estado"] == "aprobado" and data["metodo"] == "tarjeta"
    assert data["monto"] in (90, "90.00", "90")  # 100 - 10% = 90, nunca del frontend
    assert data["referencia"] == f"MOCK-{data['idPago']:06d}-{venta['nroVenta']}"
    assert data["estadoVenta"] == "registrada"


def test_no_autenticado_401(client, catalogo):
    assert client.post(BASE, json={"nroVenta": 1, "metodo": "tarjeta"}).status_code == 401
    assert client.get(f"{BASE}/1").status_code == 401


def test_venta_ajena_404(client, customer, registration):
    venta = comprar(client, [{"idVar": "v1", "cantidad": 1}])
    assert client.post("/api/auth/registro",
                       json=registration | {"correo": "otro@example.com"}).status_code == 201
    assert client.post("/api/auth/login",
                       json={"correo": "otro@example.com",
                             "contrasena": registration["contrasena"]}).status_code == 200
    assert client.post(BASE, json={"nroVenta": venta["nroVenta"], "metodo": "tarjeta"}).status_code == 404
    assert client.get(f"{BASE}/1").status_code == 404


def test_anulada_no_pagable(client, customer, factory):
    venta = comprar(client, [{"idVar": "v1", "cantidad": 1}])
    with factory.begin() as db:
        db.get(Venta, venta["nroVenta"]).estado = "anulada"
    assert client.post(BASE, json={"nroVenta": venta["nroVenta"], "metodo": "tarjeta"}).status_code == 409


@pytest.mark.parametrize("metodo", ["tarjeta", "QR", "transferencia"])
def test_metodos_permitidos(client, customer, metodo):
    venta = comprar(client, [{"idVar": "v1", "cantidad": 1}])
    assert client.post(BASE, json={"nroVenta": venta["nroVenta"], "metodo": metodo}).status_code == 201


def test_efectivo_rechazado(client, customer):
    venta = comprar(client, [{"idVar": "v1", "cantidad": 1}])
    assert client.post(BASE, json={"nroVenta": venta["nroVenta"], "metodo": "efectivo"}).status_code == 422


def test_sin_pan_ni_datos_sensibles(client, customer, factory):
    venta = comprar(client, [{"idVar": "v1", "cantidad": 1}])
    assert client.post(BASE, json={"nroVenta": venta["nroVenta"], "metodo": "tarjeta",
                                   "pan": "4111111111111111", "cvv": "123"}).status_code == 422
    data = client.post(BASE, json={"nroVenta": venta["nroVenta"], "metodo": "tarjeta"}).json()
    assert data["estado"] == "aprobado"
    with factory() as db:
        detalles = [r.detalles for r in db.scalars(select(Bitacora).where(
            Bitacora.accion.like("pago_%") | (Bitacora.accion == "venta_anulada")))]
        texto = str(detalles).lower()
        assert "pan" not in texto and "cvv" not in texto and "411111" not in texto


def test_aprobado_descuenta_movimientos_y_convierte(client, customer, factory):
    venta = comprar(client, [{"idVar": "v1", "cantidad": 2}, {"idVar": "v2", "cantidad": 1}])
    before = inventario(factory)
    data = client.post(BASE, json={"nroVenta": venta["nroVenta"], "metodo": "QR"}).json()
    assert data["estado"] == "aprobado"
    after = inventario(factory)
    assert after[0] == (1, before[0][1] - 2, before[0][2] - 2)
    assert after[1] == (2, before[1][1] - 1, before[1][2] - 1)
    movs = movimientos(factory)
    assert [(m[0], m[1], m[2]) for m in movs] == [(1, "salida", 2), (2, "salida", 1)]
    assert all(f"venta digital nro {venta['nroVenta']}" in m[3] for m in movs)
    with factory() as db:
        assert db.get(Carrito, venta["carrito"]).estado == "convertido"
        assert db.get(Venta, venta["nroVenta"]).estado == "registrada"


def test_rechazado_anula_sin_tocar(client, customer, factory):
    venta = comprar(client, [{"idVar": "v1", "cantidad": 1}])
    before = inventario(factory)
    data = client.post(BASE, json={"nroVenta": venta["nroVenta"], "metodo": "tarjeta",
                                   "escenario": "rechazado"}).json()
    assert data["estado"] == "rechazado" and data["referencia"] is None
    assert data["estadoVenta"] == "anulada"
    assert inventario(factory) == before
    assert movimientos(factory) == []
    with factory() as db:
        assert db.get(Carrito, venta["carrito"]).estado == "activo"


def test_timeout_queda_pendiente(client, customer, factory):
    venta = comprar(client, [{"idVar": "v1", "cantidad": 1}])
    before = inventario(factory)
    response = client.post(BASE, json={"nroVenta": venta["nroVenta"], "metodo": "tarjeta",
                                       "escenario": "timeout"})
    assert response.status_code == 201
    assert response.json()["estado"] == "pendiente"
    assert inventario(factory) == before
    with factory() as db:
        assert db.get(Carrito, venta["carrito"]).estado == "activo"


def test_reprocesar_pendiente_aprueba(client, customer, factory):
    venta = comprar(client, [{"idVar": "v1", "cantidad": 1}])
    pendiente = client.post(BASE, json={"nroVenta": venta["nroVenta"], "metodo": "tarjeta",
                                        "escenario": "timeout"}).json()
    final = client.post(f"{BASE}/{pendiente['idPago']}/procesar", json={}).json()
    assert final["estado"] == "aprobado" and final["referencia"] is not None
    assert inventario(factory)[0] == (1, 9, 3)


def test_doble_post_no_duplica_pendiente(client, customer, factory):
    venta = comprar(client, [{"idVar": "v1", "cantidad": 1}])
    timeout = client.post(BASE, json={"nroVenta": venta["nroVenta"], "metodo": "tarjeta",
                                      "escenario": "timeout"}).json()
    assert timeout["estado"] == "pendiente"
    reused = client.post(BASE, json={"nroVenta": venta["nroVenta"], "metodo": "QR"})
    assert reused.status_code == 200 and reused.json()["idPago"] == timeout["idPago"]
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Pago).where(
            Pago.nroventa == venta["nroVenta"])) == 1


def test_doble_aprobacion_no_descuenta_dos_veces(client, customer, factory):
    venta = comprar(client, [{"idVar": "v1", "cantidad": 1}])
    primero = client.post(BASE, json={"nroVenta": venta["nroVenta"], "metodo": "tarjeta"}).json()
    assert primero["estado"] == "aprobado"
    medio = inventario(factory)
    segundo = client.post(f"{BASE}/{primero['idPago']}/procesar", json={}).json()
    assert segundo["estado"] == "aprobado"
    assert inventario(factory) == medio
    assert len(movimientos(factory)) == 1


def test_aprobado_idempotente_entre_intentos(client, customer):
    venta = comprar(client, [{"idVar": "v1", "cantidad": 1}])
    primero = client.post(BASE, json={"nroVenta": venta["nroVenta"], "metodo": "tarjeta"}).json()
    segundo = client.post(BASE, json={"nroVenta": venta["nroVenta"], "metodo": "transferencia"}).json()
    assert segundo["idPago"] == primero["idPago"] and segundo["estado"] == "aprobado"


def test_concurrencia_ultima_unidad(client, customer, registration, factory):
    # A y B arman checkout de la última unidad; solo A cobra.
    otro = {"correo": "otro@example.com", "contrasena": registration["contrasena"]}
    assert client.post(CARRITO, json={"idVar": "v2", "cantidad": 5}).status_code == 201
    venta_a = client.post(COMPRAS, json={"nroSuc": 1}).json()
    assert client.post("/api/auth/registro",
                       json=registration | {"correo": "otro@example.com"}).status_code == 201
    assert client.post("/api/auth/login", json=otro).status_code == 200
    assert client.post(CARRITO, json={"idVar": "v2", "cantidad": 5}).status_code == 201
    venta_b = client.post(COMPRAS, json={"nroSuc": 1}).json()
    assert client.post("/api/auth/login", json={
        "correo": registration["correo"], "contrasena": registration["contrasena"]}).status_code == 200
    assert client.post(BASE, json={"nroVenta": venta_a["nroVenta"], "metodo": "tarjeta"}).status_code == 201
    assert client.post("/api/auth/login", json=otro).status_code == 200
    respuesta_b = client.post(BASE, json={"nroVenta": venta_b["nroVenta"], "metodo": "tarjeta"})
    assert respuesta_b.status_code == 409  # autorizado por el mock, sin stock: pendiente
    with factory() as db:
        pago_b = db.scalar(select(Pago).where(Pago.nroventa == venta_b["nroVenta"]))
        assert pago_b.estado == "pendiente"
        assert db.get(Venta, venta_b["nroVenta"]).estado == "registrada"
    assert inventario(factory)[1] == (2, 0, 0)


def test_falta_stock_antes_de_aprobar(client, customer, factory):
    venta = comprar(client, [{"idVar": "v1", "cantidad": 2}])
    with factory.begin() as db:
        db.get(Inventario, 1).cantdisp = 0
        db.get(Inventario, 1).stock = 0
    assert client.post(BASE, json={"nroVenta": venta["nroVenta"], "metodo": "tarjeta"}).status_code == 409


def test_varias_filas_greedy(client, customer, factory):
    venta = comprar(client, [{"idVar": "v3", "cantidad": 3}])
    data = client.post(BASE, json={"nroVenta": venta["nroVenta"], "metodo": "transferencia"}).json()
    assert data["estado"] == "aprobado"
    assert inventario(factory)[2:] == [(3, 0, 0), (4, 3, 0)]
    movs = [m for m in movimientos(factory) if m[0] in (3, 4)]
    assert [(m[0], m[2]) for m in movs] == [(3, 1), (4, 2)]


def test_get_pago_propio(client, customer):
    venta = comprar(client, [{"idVar": "v1", "cantidad": 1}])
    creado = client.post(BASE, json={"nroVenta": venta["nroVenta"], "metodo": "tarjeta"}).json()
    visto = client.get(f"{BASE}/{creado['idPago']}").json()
    assert visto == creado


def test_bearer_movil(client, customer, settings):
    venta = comprar(client, [{"idVar": "v1", "cantidad": 1}])
    headers = bearer(client, settings)
    client.headers.pop("X-CSRF-Protection", None)
    client.headers.pop("Origin", None)
    try:
        assert client.post(BASE, json={"nroVenta": venta["nroVenta"], "metodo": "QR"},
                           headers=headers).status_code == 201
    finally:
        client.headers.update({"Origin": "http://localhost:4200", "X-CSRF-Protection": "1"})


def test_cookie_mantiene_csrf(client, customer):
    venta = comprar(client, [{"idVar": "v1", "cantidad": 1}])
    client.headers.pop("X-CSRF-Protection", None)
    try:
        assert client.post(BASE, json={"nroVenta": venta["nroVenta"], "metodo": "tarjeta"}).status_code == 403
    finally:
        client.headers.update({"X-CSRF-Protection": "1"})


def test_escenario_solo_fuera_de_produccion(factory):
    with pytest.raises(DomainError) as error:
        service.iniciar(factory(), PagoCrear(nroVenta=1, metodo="tarjeta", escenario="timeout"),
                        "nadie", None, PasarelaMock(), "production")
    assert error.value.status == 422
