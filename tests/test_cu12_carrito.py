"""CU12 Carrito: HTTP/domain regression con SQLite inyectado."""
from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql

from app.modules.cliente_experiencia_compra.shared.models.catalogo import (
    Categoria,
    Coleccion,
    Color,
    Inventario,
    Marca,
    Producto,
    Promocion,
    Proveedor,
    Talla,
    VarianteColor,
    VarianteProd,
)
from app.modules.cliente_experiencia_compra.shared.models.comercio import Carrito, DetalleCarro
from app.modules.cliente_experiencia_compra.cu12_carrito.repositories import carrito as repository
from app.modules.seguridad_accesos.models import Bitacora, Cliente, Usuario
from app.modules.seguridad_accesos.shared.models import Ciudad, Sucursal

BASE = "/api/cliente/carrito"


@pytest.fixture
def catalogo(factory):
    with factory.begin() as db:
        for row in (Categoria(idcat=1, descripcion="Camisas"),
                    Marca(idmarca=1, nombre="Andes", estado="activo"),
                    Coleccion(idcol=1, descripcion="Otoño"),
                    Proveedor(idprov=1, nombre="Prov", correo="p@example.com",
                              direccion="Dir", telefono="70000000"),
                    Promocion(idpromo=1, nombre="Promo", descripcion="D",
                              tipodescuento="porcentaje", valordescuento=10,
                              fechaini=date(2026, 1, 1), fechafin=date(2026, 12, 31),
                              estado="activo"),
                    Talla(idtalla=1, descripcion="M"), Talla(idtalla=2, descripcion="L"),
                    Color(idcolor=1, descripcion="Rojo", hex="#FF0000"),
                    Ciudad(id=1, nombre="La Paz")):
            db.add(row)
        db.flush()
        for row in (Producto(idprod="p1", descripcion="Camisa", estado="activo",
                             idpromo=1, idcat=1, idmarca=1, idcol=1, idprov=1),
                    Producto(idprod="p-off", descripcion="Baja", estado="inactivo",
                             idpromo=None, idcat=1, idmarca=1, idcol=1, idprov=1),
                    Sucursal(nro=1, nombre="Central", direccion="Dir 1", estado="activo", idciud=1)):
            db.add(row)
        db.flush()
        for row in (VarianteProd(idvariante="v1", sku="SKU-1", precio=100,
                                 estado="activo", img="http://img/1.jpg", idtalla=1, idprod="p1"),
                    VarianteProd(idvariante="v2", sku="SKU-2", precio=150,
                                 estado="activo", img=None, idtalla=2, idprod="p1"),
                    VarianteProd(idvariante="v-off", sku="SKU-OFF", precio=50,
                                 estado="inactivo", img=None, idtalla=1, idprod="p1"),
                    VarianteProd(idvariante="v-poff", sku="SKU-POFF", precio=50,
                                 estado="activo", img=None, idtalla=1, idprod="p-off")):
            db.add(row)
        db.flush()
        for row in (VarianteColor(idvar="v1", idcolor=1),
                    Inventario(nroinv=1, stock=10, cantdisp=4, nrosuc=1, idvar="v1"),
                    Inventario(nroinv=2, stock=5, cantdisp=1, nrosuc=1, idvar="v2")):
            db.add(row)
        db.flush()


@pytest.fixture
def customer(client, registered, credentials, catalogo):
    assert client.post("/api/auth/login", json=credentials).status_code == 200
    return registered["idUsuario"]


def inventario(factory):
    with factory() as db:
        return [(r.nroinv, r.stock, r.cantdisp) for r in
                db.scalars(select(Inventario).order_by(Inventario.nroinv))]


def carritos_activos(factory, user_id):
    with factory() as db:
        return list(db.scalars(select(Carrito).where(
            Carrito.idusuariocl == user_id, Carrito.estado == "activo")))


def bearer(client, settings):
    token = client.cookies.get(settings.cookie_name)
    assert token and len(token) == 43
    client.cookies.clear()
    return {"Authorization": f"Bearer {token}"}


def test_routes_require_authentication(client, catalogo):
    assert client.post(BASE + "/items", json={"idVar": "v1", "cantidad": 1}).status_code == 401
    assert client.get(BASE).status_code == 401
    assert client.patch(BASE + "/items/1", json={"cantidad": 2}).status_code == 401
    assert client.delete(BASE + "/items/1").status_code == 401


def test_non_cliente_rejected(client, registered, credentials, catalogo, factory):
    with factory.begin() as db:
        user = db.get(Usuario, registered["idUsuario"])
        db.delete(db.get(Cliente, user.idusuario))
        user.tipo, user.nrorol = "E", "cliente"
    assert client.post("/api/auth/login", json=credentials).status_code == 200
    assert client.get(BASE).status_code == 403
    assert client.post(BASE + "/items", json={"idVar": "v1", "cantidad": 1}).status_code == 403


def test_get_vacio_sin_insert(client, customer, factory):
    body = client.get(BASE).json()
    assert body["idCarrito"] is None and body["items"] == [] and body["cantidadItems"] == 0
    assert body["subtotal"] in ("0", 0, 0.0)
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Carrito)) == 0
    # Segunda lectura: sigue sin crear nada.
    assert client.get(BASE).json()["idCarrito"] is None
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Carrito)) == 0


def test_primer_post_crea_activo(client, customer, factory):
    response = client.post(BASE + "/items", json={"idVar": "v1", "cantidad": 2})
    assert response.status_code == 201
    data = response.json()
    assert data["idCarrito"] is not None and len(data["items"]) == 1
    with factory() as db:
        cart = db.get(Carrito, data["idCarrito"])
        assert cart.estado == "activo"


def test_segundo_item_reutiliza_carrito(client, customer):
    first = client.post(BASE + "/items", json={"idVar": "v1", "cantidad": 1}).json()
    second = client.post(BASE + "/items", json={"idVar": "v2", "cantidad": 1}).json()
    assert first["idCarrito"] == second["idCarrito"]
    assert len(second["items"]) == 2


def test_misma_variante_incrementa(client, customer):
    client.post(BASE + "/items", json={"idVar": "v1", "cantidad": 1})
    data = client.post(BASE + "/items", json={"idVar": "v1", "cantidad": 2}).json()
    assert len(data["items"]) == 1 and data["items"][0]["cantidad"] == 3
    assert data["cantidadItems"] == 3


def test_suma_sin_lost_update(client, customer):
    client.post(BASE + "/items", json={"idVar": "v1", "cantidad": 1})
    client.post(BASE + "/items", json={"idVar": "v1", "cantidad": 1})
    data = client.get(BASE).json()
    assert data["items"][0]["cantidad"] == 2


def test_bloqueos_for_update_en_postgres():
    from sqlalchemy import select as _select
    q1 = _select(Carrito).where(Carrito.idusuariocl == "x").with_for_update()
    assert "FOR UPDATE" in str(q1.compile(dialect=postgresql.dialect()))
    q2 = _select(DetalleCarro).where(DetalleCarro.idcarrito == 1).with_for_update()
    assert "FOR UPDATE" in str(q2.compile(dialect=postgresql.dialect()))
    assert repository.locked_cliente is not None and repository.locked_active_cart is not None


def test_cantidad_invalida(client, customer):
    assert client.post(BASE + "/items", json={"idVar": "v1", "cantidad": 0}).status_code == 422
    assert client.post(BASE + "/items", json={"idVar": "v1", "cantidad": -2}).status_code == 422
    assert client.post(BASE + "/items", json={"idVar": "v1"}).status_code == 422


def test_variante_inexistente(client, customer):
    assert client.post(BASE + "/items", json={"idVar": "zzz", "cantidad": 1}).status_code == 404


def test_producto_inactivo(client, customer):
    assert client.post(BASE + "/items", json={"idVar": "v-poff", "cantidad": 1}).status_code == 404


def test_variante_inactiva(client, customer):
    assert client.post(BASE + "/items", json={"idVar": "v-off", "cantidad": 1}).status_code == 404


def test_disponibilidad_insuficiente(client, customer, factory):
    assert client.post(BASE + "/items", json={"idVar": "v1", "cantidad": 5}).status_code == 409
    assert client.get(BASE).json()["items"] == []


def test_operaciones_no_modifican_inventario(client, customer, factory):
    before = inventario(factory)
    created = client.post(BASE + "/items", json={"idVar": "v1", "cantidad": 2}).json()
    assert client.patch(f"{BASE}/items/{created['items'][0]['idDetalleCarro']}",
                        json={"cantidad": 3}).status_code == 200
    assert client.delete(f"{BASE}/items/{created['items'][0]['idDetalleCarro']}").status_code == 200
    assert inventario(factory) == before


def test_patch_cantidad_absoluta(client, customer):
    created = client.post(BASE + "/items", json={"idVar": "v1", "cantidad": 1}).json()
    detail_id = created["items"][0]["idDetalleCarro"]
    updated = client.patch(f"{BASE}/items/{detail_id}", json={"cantidad": 3}).json()
    assert updated["items"][0]["cantidad"] == 3
    assert updated["cantidadItems"] == 3
    # Hay dos variantes registradas, pero cuatro unidades disponibles de v1.
    # La cantidad se limita por inventario, no por el número de variantes.
    maximum = client.patch(f"{BASE}/items/{detail_id}", json={"cantidad": 4})
    assert maximum.status_code == 200
    assert maximum.json()["items"][0]["cantidad"] == 4
    reduced = client.patch(f"{BASE}/items/{detail_id}", json={"cantidad": 2})
    assert reduced.status_code == 200
    assert client.get(BASE).json()["items"][0]["cantidad"] == 2


def test_patch_valida_disponibilidad(client, customer):
    created = client.post(BASE + "/items", json={"idVar": "v1", "cantidad": 1}).json()
    detail_id = created["items"][0]["idDetalleCarro"]
    assert client.patch(f"{BASE}/items/{detail_id}", json={"cantidad": 5}).status_code == 409
    assert client.get(BASE).json()["items"][0]["cantidad"] == 1


def test_editar_resumen_sin_pago_no_cancela_carrito(client, customer, factory):
    created = client.post(BASE + "/items", json={"idVar": "v1", "cantidad": 2}).json()
    venta = client.post("/api/cliente/compras/desde-carrito", json={"nroSuc": 1}).json()
    detail_id = created["items"][0]["idDetalleCarro"]
    invalid = client.patch(f"{BASE}/items/{detail_id}", json={"cantidad": 5})
    assert invalid.status_code == 409
    assert client.get(f"/api/cliente/compras/{venta['nroVenta']}").json()["estado"] == "registrada"
    response = client.patch(f"{BASE}/items/{detail_id}", json={"cantidad": 3})
    assert response.status_code == 200
    assert response.json()["idCarrito"] == created["idCarrito"]
    assert response.json()["items"][0]["cantidad"] == 3
    assert client.get("/api/cliente/compras/pendiente").json() is None
    assert client.get(f"/api/cliente/compras/{venta['nroVenta']}").json()["estado"] == "anulada"
    replacement = client.post("/api/cliente/compras/desde-carrito", json={"nroSuc": 1}).json()
    assert replacement["nroVenta"] != venta["nroVenta"]
    assert replacement["items"][0]["cantidad"] == 3
    assert replacement["total"] in (270, "270.00")
    assert inventario(factory)[0][1:] == (10, 4)


def test_editar_carrito_con_pago_en_curso_se_rechaza(client, customer):
    created = client.post(BASE + "/items", json={"idVar": "v1", "cantidad": 2}).json()
    venta = client.post("/api/cliente/compras/desde-carrito", json={"nroSuc": 1}).json()
    response = client.post("/api/cliente/pagos", json={"nroVenta": venta["nroVenta"], "metodo": "tarjeta", "escenario": "timeout"})
    assert response.status_code == 201
    assert response.json()["estado"] == "pendiente"
    detail_id = created["items"][0]["idDetalleCarro"]
    for response in (client.patch(f"{BASE}/items/{detail_id}", json={"cantidad": 1}),
                     client.delete(f"{BASE}/items/{detail_id}")):
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "pago_en_curso"
    assert client.get(BASE).json()["items"][0]["cantidad"] == 2


def test_patch_cantidad_invalida(client, customer):
    created = client.post(BASE + "/items", json={"idVar": "v1", "cantidad": 1}).json()
    detail_id = created["items"][0]["idDetalleCarro"]
    assert client.patch(f"{BASE}/items/{detail_id}", json={"cantidad": 0}).status_code == 422


def test_delete_mantiene_activo_vacio(client, customer, factory):
    created = client.post(BASE + "/items", json={"idVar": "v1", "cantidad": 1}).json()
    cart_id, detail_id = created["idCarrito"], created["items"][0]["idDetalleCarro"]
    deleted = client.delete(f"{BASE}/items/{detail_id}").json()
    assert deleted["idCarrito"] == cart_id and deleted["items"] == [] and deleted["cantidadItems"] == 0
    assert deleted["subtotal"] in ("0", 0, 0.0)
    with factory() as db:
        assert db.get(Carrito, cart_id).estado == "activo"


def test_delete_inexistente_404(client, customer):
    assert client.delete(f"{BASE}/items/999").status_code == 404
    assert client.patch(f"{BASE}/items/999", json={"cantidad": 1}).status_code == 404


def test_recurso_ajeno_404(client, customer, registration):
    detail_id = client.post(BASE + "/items", json={"idVar": "v1", "cantidad": 1}).json()["items"][0]["idDetalleCarro"]
    assert client.post("/api/auth/registro",
                       json=registration | {"correo": "otro@example.com"}).status_code == 201
    assert client.post("/api/auth/login",
                       json={"correo": "otro@example.com",
                             "contrasena": registration["contrasena"]}).status_code == 200
    vacio = client.get(BASE).json()
    assert vacio["idCarrito"] is None and vacio["items"] == [] and vacio["cantidadItems"] == 0
    assert client.patch(f"{BASE}/items/{detail_id}", json={"cantidad": 2}).status_code == 404
    assert client.delete(f"{BASE}/items/{detail_id}").status_code == 404


def test_lectura_enriquecida_y_subtotales(client, customer):
    client.post(BASE + "/items", json={"idVar": "v1", "cantidad": 2})
    client.post(BASE + "/items", json={"idVar": "v2", "cantidad": 1})
    data = client.get(BASE).json()
    assert data["cantidadItems"] == 3
    assert data["subtotal"] in (350, "350.00", "350")
    first = next(i for i in data["items"] if i["idVar"] == "v1")
    assert first["sku"] == "SKU-1" and first["producto"] == "Camisa"
    assert first["talla"] == {"idTalla": 1, "descripcion": "M"}
    assert first["colores"] == [{"idColor": 1, "descripcion": "Rojo", "hex": "#FF0000"}]
    assert first["imagen"] == "http://img/1.jpg"
    assert first["promocion"]["idPromo"] == 1
    assert first["subtotal"] in (200, "200.00", "200")
    assert first["disponible"] is True and first["cantidadDisponible"] == 4


def test_inactivo_en_carrito_no_disponible(client, customer, factory):
    created = client.post(BASE + "/items", json={"idVar": "v1", "cantidad": 1}).json()
    detail_id = created["items"][0]["idDetalleCarro"]
    with factory.begin() as db:
        db.get(VarianteProd, "v1").estado = "inactivo"
    data = client.get(BASE).json()
    assert len(data["items"]) == 1 and data["items"][0]["disponible"] is False
    assert client.patch(f"{BASE}/items/{detail_id}", json={"cantidad": 2}).status_code == 404
    assert client.delete(f"{BASE}/items/{detail_id}").status_code == 200


def test_convertido_bloquea_y_post_crea_nuevo(client, customer, factory):
    created = client.post(BASE + "/items", json={"idVar": "v1", "cantidad": 1}).json()
    old_cart, detail_id = created["idCarrito"], created["items"][0]["idDetalleCarro"]
    with factory.begin() as db:
        db.get(Carrito, old_cart).estado = "convertido"
    assert client.patch(f"{BASE}/items/{detail_id}", json={"cantidad": 2}).status_code == 404
    assert client.delete(f"{BASE}/items/{detail_id}").status_code == 404
    assert client.get(BASE).json()["idCarrito"] is None
    nuevo = client.post(BASE + "/items", json={"idVar": "v1", "cantidad": 1}).json()
    assert nuevo["idCarrito"] != old_cart


def test_dos_posts_un_solo_activo(client, customer, factory):
    client.post(BASE + "/items", json={"idVar": "v1", "cantidad": 1})
    client.post(BASE + "/items", json={"idVar": "v2", "cantidad": 1})
    assert len(carritos_activos(factory, customer)) == 1


def test_bitacora_carrito(client, customer, factory):
    created = client.post(BASE + "/items", json={"idVar": "v1", "cantidad": 1}).json()
    detail_id = created["items"][0]["idDetalleCarro"]
    assert client.patch(f"{BASE}/items/{detail_id}", json={"cantidad": 2}).status_code == 200
    assert client.delete(f"{BASE}/items/{detail_id}").status_code == 200
    with factory() as db:
        actions = [r.accion for r in db.scalars(
            select(Bitacora).where(Bitacora.usuario_id == customer).order_by(Bitacora.id))]
    assert "carrito_item_agregado" in actions
    assert "carrito_item_actualizado" in actions
    assert "carrito_item_eliminado" in actions


def test_bearer_movil_post(client, customer, settings):
    headers = bearer(client, settings)
    client.headers.pop("X-CSRF-Protection", None)
    client.headers.pop("Origin", None)
    try:
        assert client.post(BASE + "/items", json={"idVar": "v1", "cantidad": 1},
                           headers=headers).status_code == 201
    finally:
        client.headers.update({"Origin": "http://localhost:4200", "X-CSRF-Protection": "1"})


def test_cookie_mantiene_csrf(client, customer):
    client.headers.pop("X-CSRF-Protection", None)
    try:
        assert client.post(BASE + "/items", json={"idVar": "v1", "cantidad": 1}).status_code == 403
    finally:
        client.headers.update({"X-CSRF-Protection": "1"})
