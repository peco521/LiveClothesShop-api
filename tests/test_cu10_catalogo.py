"""CU10 Consultar Prendas: HTTP/domain regression con SQLite inyectado."""
from datetime import date

import pytest

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
    TempColeccion,
    Temporada,
    VarianteColor,
    VarianteProd,
)
from app.modules.seguridad_accesos.models import Cliente, Rol, Usuario
from app.modules.seguridad_accesos.shared.models import Ciudad, Sucursal

BASE = "/api/catalogo/productos"


@pytest.fixture
def catalog(factory):
    # NOTA: seeding por niveles con flush intermedio. Un único flush
    # combinado emite INSERTs fuera de orden bajo SQLite en este entorno;
    # CU10 es solo lectura y nunca escribe estas tablas en producción.
    with factory.begin() as db:
        for row in (Categoria(idcat=1, descripcion="Camisas"),
                    Categoria(idcat=2, descripcion="Pantalones"),
                    Marca(idmarca=1, nombre="Andes", estado="activo"),
                    Coleccion(idcol=1, descripcion="Otoño"),
                    Temporada(idtemp=1, nombre="Otoño-Invierno", fechaini=date(2026, 3, 1),
                              fechafin=date(2026, 9, 30), estado="activo"),
                    Proveedor(idprov=1, nombre="Prov", correo="p@example.com",
                              direccion="Dir", telefono="70000000"),
                    Talla(idtalla=1, descripcion="M"),
                    Talla(idtalla=2, descripcion="L"),
                    Color(idcolor=1, descripcion="Rojo", hex="#FF0000"),
                    Color(idcolor=2, descripcion="Azul", hex="#0000FF"),
                    Ciudad(id=1, nombre="La Paz"),
                    Promocion(idpromo=1, nombre="Promo", descripcion="Desc",
                              tipodescuento="porcentaje", valordescuento=10,
                              fechaini=date(2026, 1, 1), fechafin=date(2026, 12, 31),
                              estado="activo")):
            db.add(row)
        db.flush()
        for row in (TempColeccion(idtemp=1, idcol=1),
                    Producto(idprod="prod-001", descripcion="Camisa Oxford", estado="activo",
                             idpromo=1, idcat=1, idmarca=1, idcol=1, idprov=1),
                    Producto(idprod="prod-002", descripcion="Pantalón Chino", estado="activo",
                             idpromo=None, idcat=2, idmarca=1, idcol=1, idprov=1),
                    Producto(idprod="prod-off", descripcion="Prenda dada de baja", estado="inactivo",
                             idpromo=None, idcat=1, idmarca=1, idcol=1, idprov=1),
                    Sucursal(nro=1, nombre="Central", direccion="Dir 1", estado="activo", idciud=1),
                    Sucursal(nro=2, nombre="Cerrada", direccion="Dir 2", estado="inactivo", idciud=1)):
            db.add(row)
        db.flush()
        for row in (VarianteProd(idvariante="var-001", sku="SKU-001", precio=100,
                                 estado="activo", img="http://img/1.jpg", idtalla=1, idprod="prod-001"),
                    VarianteProd(idvariante="var-002", sku="SKU-002", precio=150,
                                 estado="activo", img=None, idtalla=2, idprod="prod-001"),
                    VarianteProd(idvariante="var-off", sku="SKU-OFF", precio=50,
                                 estado="inactivo", img=None, idtalla=1, idprod="prod-001"),
                    VarianteProd(idvariante="var-003", sku="SKU-003", precio=200,
                                 estado="activo", img=None, idtalla=1, idprod="prod-002")):
            db.add(row)
        db.flush()
        for row in (VarianteColor(idvar="var-001", idcolor=1),
                    VarianteColor(idvar="var-002", idcolor=2),
                    VarianteColor(idvar="var-003", idcolor=1)):
            db.add(row)
        db.flush()
        for row in (Inventario(nroinv=1, stock=10, cantdisp=4, nrosuc=1, idvar="var-001"),
                    Inventario(nroinv=2, stock=5, cantdisp=0, nrosuc=1, idvar="var-002"),
                    Inventario(nroinv=3, stock=7, cantdisp=9, nrosuc=2, idvar="var-001")):
            db.add(row)
        db.flush()


@pytest.fixture
def customer(client, registered, credentials, catalog):
    assert client.post("/api/auth/login", json=credentials).status_code == 200
    return registered["idUsuario"]


def inventory_snapshot(factory):
    with factory() as db:
        return [(r.nroinv, r.stock, r.cantdisp) for r in
                db.scalars(__import__("sqlalchemy").select(Inventario).order_by(Inventario.nroinv))]


def test_category_brands_exclude_unrelated_and_inactive(client, customer, factory):
    with factory.begin() as db:
        db.add_all([
            Marca(idmarca=2, nombre="Otra categoría", estado="activo"),
            Marca(idmarca=3, nombre="Marca inactiva", estado="inactivo"),
            Marca(idmarca=4, nombre="Solo prendas inactivas", estado="activo"),
        ])
        db.flush()
        db.add_all([
            Producto(idprod="extra-2", descripcion="Otra", estado="activo", idcat=2, idmarca=2, idcol=1, idprov=1),
            Producto(idprod="extra-3", descripcion="Otra", estado="activo", idcat=1, idmarca=3, idcol=1, idprov=1),
            Producto(idprod="extra-4", descripcion="Otra", estado="inactivo", idcat=1, idmarca=4, idcol=1, idprov=1),
        ])
    response = client.get("/api/catalogo/marcas", params={"idCat": 1})
    assert response.status_code == 200
    assert response.json() == {"items": [{"id": 1, "nombre": "Andes"}], "total": 1}
    assert client.get("/api/catalogo/marcas", params={"idCat": 999}).json()["items"] == []
    assert client.get("/api/catalogo/marcas", params={"idCat": 0}).status_code == 422


def test_routes_require_authentication(client, catalog):
    assert client.get(BASE).status_code == 401
    assert client.get(BASE + "/prod-001").status_code == 401
    assert client.get("/api/catalogo/categorias").status_code == 401


def test_non_cliente_rejected(client, registered, credentials, catalog, factory):
    with factory.begin() as db:
        user = db.get(Usuario, registered["idUsuario"])
        db.delete(db.get(Cliente, user.idusuario))
        user.tipo, user.nrorol = "E", "cliente"
    assert client.post("/api/auth/login", json=credentials).status_code == 200
    assert client.get(BASE).status_code == 403
    assert client.get(BASE + "/prod-001").status_code == 403


def test_bearer_transport_for_mobile(client, customer, settings):
    token = client.cookies.get(settings.cookie_name)
    assert token and len(token) == 43
    client.cookies.clear()
    assert client.get(BASE, headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert client.get(BASE + "/prod-001", headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_list_pagination_and_search(client, customer):
    body = client.get(BASE).json()
    assert body["total"] == 2 and len(body["items"]) == 2
    assert body["offset"] == 0 and body["limit"] == 20
    page = client.get(BASE, params={"offset": 1, "limit": 1}).json()
    assert len(page["items"]) == 1 and page["total"] == 2
    assert client.get(BASE, params={"q": "oxford"}).json()["total"] == 1
    assert client.get(BASE, params={"q": "ANDES"}).json()["total"] == 2
    assert client.get(BASE, params={"q": "inexistente-zzz"}).json() == {
        "items": [], "total": 0, "offset": 0, "limit": 20}


def test_filters(client, customer):
    assert client.get(BASE, params={"idCat": 1}).json()["total"] == 1
    assert client.get(BASE, params={"idMarca": 1}).json()["total"] == 2
    assert client.get(BASE, params={"idCol": 1}).json()["total"] == 2
    assert client.get(BASE, params={"idTemp": 1}).json()["total"] == 2
    assert client.get(BASE, params={"idTemp": 999}).json()["total"] == 0
    assert client.get(BASE, params={"idTalla": 2}).json()["total"] == 1
    assert client.get(BASE, params={"idColor": 2}).json()["total"] == 1
    assert client.get(BASE, params={"minPrecio": 150}).json()["total"] == 2
    assert client.get(BASE, params={"maxPrecio": 100}).json()["total"] == 1
    assert client.get(BASE, params={"minPrecio": 500}).json()["total"] == 0
    # prod-001 tiene cantDisp>0 en sucursal activa; prod-002 no tiene inventario.
    assert client.get(BASE, params={"soloDisponibles": True}).json()["total"] == 1
    ordered = client.get(BASE, params={"sort": "precio_desc"}).json()["items"]
    assert ordered[0]["idProd"] == "prod-002"


def test_invalid_filters(client, customer):
    assert client.get(BASE, params={"minPrecio": 200, "maxPrecio": 100}).status_code == 422
    assert client.get(BASE, params={"sort": "novedades"}).status_code == 422
    assert client.get(BASE, params={"limit": 500}).status_code == 422


def test_list_item_shape_and_promo(client, customer):
    body = client.get(BASE, params={"q": "oxford"}).json()
    item = body["items"][0]
    assert item["idProd"] == "prod-001"
    assert item["categoria"] == {"idCat": 1, "descripcion": "Camisas"}
    assert item["marca"] == {"idMarca": 1, "nombre": "Andes"}
    assert item["coleccion"] == {"idCol": 1, "descripcion": "Otoño"}
    assert item["promocion"]["idPromo"] == 1
    assert item["precioMin"] == "100.00" or item["precioMin"] == 100
    assert item["disponible"] is True and item["totalVariantes"] == 2
    plain = client.get(BASE, params={"q": "chino"}).json()["items"][0]
    assert plain["promocion"] is None and plain["disponible"] is False


def test_detail(client, customer):
    body = client.get(BASE + "/prod-001").json()
    assert body["idProd"] == "prod-001" and body["estado"] == "activo"
    assert len(body["variantes"]) == 2  # la variante inactiva queda excluida
    first = next(v for v in body["variantes"] if v["idVariante"] == "var-001")
    assert first["talla"] == {"idTalla": 1, "descripcion": "M"}
    assert first["colores"] == [{"idColor": 1, "descripcion": "Rojo", "hex": "#FF0000"}]
    assert first["imagen"] == "http://img/1.jpg"
    branches = [d for d in body["disponibilidad"] if d["idVariante"] == "var-001"]
    assert any(d["nroSuc"] == 1 and d["cantDisp"] == 4 and d["ciudad"] == "La Paz" for d in branches)
    # Sucursal inactiva no se expone.
    assert all(d["nroSuc"] != 2 for d in body["disponibilidad"])


def test_detail_not_found_and_inactive(client, customer):
    assert client.get(BASE + "/missing").status_code == 404
    assert client.get(BASE + "/prod-off").status_code == 404


def test_read_only_does_not_touch_inventory(client, customer, factory):
    before = inventory_snapshot(factory)
    assert client.get(BASE).status_code == 200
    assert client.get(BASE + "/prod-001").status_code == 200
    assert client.get(BASE + "/prod-002").status_code == 200
    assert inventory_snapshot(factory) == before


def test_facets(client, customer):
    expected = {"categorias": 2, "marcas": 1, "colecciones": 1,
                "temporadas": 1, "tallas": 2, "colores": 2}
    for group, total in expected.items():
        body = client.get("/api/catalogo/" + group).json()
        assert body["total"] == total, group
        assert all(set(item) == {"id", "nombre"} for item in body["items"]), group
    names = [item["nombre"] for item in client.get("/api/catalogo/categorias").json()["items"]]
    assert names == ["Camisas", "Pantalones"]
    assert client.get("/api/catalogo/inexistente").status_code == 404
