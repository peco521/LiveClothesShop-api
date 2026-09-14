"""CU13 Compra digital: HTTP/domain regression con SQLite inyectado."""
from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

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
    DetalleVenta,
    MovimientoInv,
    Venta,
)
from app.modules.seguridad_accesos.models import Bitacora, Cliente, Usuario
from app.modules.seguridad_accesos.shared.models import Ciudad, Sucursal

BASE = "/api/cliente/compras"
CARRITO = "/api/cliente/carrito/items"


def num(value):
    from decimal import Decimal
    return Decimal(str(value))


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
                    Promocion(idpromo=2, nombre="Fijo30", descripcion="D",
                              tipodescuento="montoFijo", valordescuento=30,
                              fechaini=date(2026, 1, 1), fechafin=date(2026, 12, 31),
                              estado="activo"),
                    Promocion(idpromo=3, nombre="Fijo50", descripcion="D",
                              tipodescuento="montoFijo", valordescuento=50,
                              fechaini=date(2026, 1, 1), fechafin=date(2026, 12, 31),
                              estado="activo"),
                    Talla(idtalla=1, descripcion="M"), Talla(idtalla=2, descripcion="L"),
                    Ciudad(id=1, nombre="La Paz")):
            db.add(row)
        db.flush()
        for row in (Producto(idprod="p1", descripcion="Camisa", estado="activo",
                             idpromo=1, idcat=1, idmarca=1, idcol=1, idprov=1),
                    Producto(idprod="p2", descripcion="Chamarra", estado="activo",
                             idpromo=2, idcat=1, idmarca=1, idcol=1, idprov=1),
                    Producto(idprod="p3", descripcion="Medias", estado="activo",
                             idpromo=3, idcat=1, idmarca=1, idcol=1, idprov=1),
                    Sucursal(nro=1, nombre="Central", direccion="Dir 1", estado="activo", idciud=1),
                    Sucursal(nro=2, nombre="Sur", direccion="Dir 2", estado="activo", idciud=1),
                    Sucursal(nro=3, nombre="Cerrada", direccion="Dir 3", estado="inactivo", idciud=1)):
            db.add(row)
        db.flush()
        for row in (VarianteProd(idvariante="v1", sku="SKU-1", precio=100,
                                 estado="activo", img=None, idtalla=1, idprod="p1"),
                    VarianteProd(idvariante="v2", sku="SKU-2", precio=150,
                                 estado="activo", img=None, idtalla=2, idprod="p1"),
                    VarianteProd(idvariante="v3", sku="SKU-3", precio=200,
                                 estado="activo", img=None, idtalla=1, idprod="p2"),
                    VarianteProd(idvariante="v4", sku="SKU-4", precio=20,
                                 estado="activo", img=None, idtalla=1, idprod="p3")):
            db.add(row)
        db.flush()
        for row in (Inventario(nroinv=1, stock=10, cantdisp=2, nrosuc=1, idvar="v1"),
                    Inventario(nroinv=2, stock=8, cantdisp=5, nrosuc=2, idvar="v1"),
                    Inventario(nroinv=3, stock=5, cantdisp=5, nrosuc=1, idvar="v2"),
                    Inventario(nroinv=4, stock=4, cantdisp=3, nrosuc=1, idvar="v3"),
                    Inventario(nroinv=5, stock=6, cantdisp=2, nrosuc=1, idvar="v4")):
            db.add(row)
        db.flush()


@pytest.fixture
def customer(client, registered, credentials, catalogo):
    assert client.post("/api/auth/login", json=credentials).status_code == 200
    return registered["idUsuario"]


def llenar(client, items):
    for item in items:
        assert client.post(CARRITO, json=item).status_code == 201


def inventario(factory):
    with factory() as db:
        return [(r.nroinv, r.stock, r.cantdisp) for r in
                db.scalars(select(Inventario).order_by(Inventario.nroinv))]


def bearer(client, settings):
    token = client.cookies.get(settings.cookie_name)
    assert token and len(token) == 43
    client.cookies.clear()
    return {"Authorization": f"Bearer {token}"}


def test_preparar_venta_valida(client, customer, factory):
    llenar(client, [{"idVar": "v1", "cantidad": 2}, {"idVar": "v2", "cantidad": 1}])
    response = client.post(BASE + "/desde-carrito", json={"nroSuc": 1})
    assert response.status_code == 201
    data = response.json()
    assert data["estado"] == "registrada" and data["nit"] is None
    assert data["sucursal"] == {"nro": 1, "nombre": "Central", "ciudad": "La Paz"}
    assert [(i["idVar"], i["cantidad"], num(i["precioUnitario"])) for i in data["items"]] == [
        ("v1", 2, 100), ("v2", 1, 150)]
    assert data["brutoTotal"] in (350, "350.00", "350")
    assert data["descAplicado"] in (35, "35.00", "35")
    assert data["total"] in (315, "315.00", "315")
    with factory() as db:
        venta = db.get(Venta, data["nroVenta"])
        assert venta.estado == "registrada" and venta.nroreserva is None
        assert venta.idusuarioemp is None and venta.idcarrito == data["carrito"]
        detalles = db.scalars(select(DetalleVenta).where(
            DetalleVenta.nroventa == venta.nroventa)).all()
        assert [(d.idvar, d.cantidad, d.preciounitario) for d in detalles] == [
            ("v1", 2, 100), ("v2", 1, 150)]


def test_non_cliente_rechazado(client, registered, credentials, catalogo, factory):
    with factory.begin() as db:
        user = db.get(Usuario, registered["idUsuario"])
        db.delete(db.get(Cliente, user.idusuario))
        user.tipo, user.nrorol = "E", "cliente"
    assert client.post("/api/auth/login", json=credentials).status_code == 200
    assert client.post(BASE + "/desde-carrito", json={"nroSuc": 1}).status_code == 403


def test_carrito_inexistente_y_vacio(client, customer):
    assert client.post(BASE + "/desde-carrito", json={"nroSuc": 1}).status_code == 409
    llenar(client, [{"idVar": "v1", "cantidad": 1}])
    detail = client.get("/api/cliente/carrito").json()["items"][0]
    assert client.delete(f"/api/cliente/carrito/items/{detail['idDetalleCarro']}").status_code == 200
    assert client.post(BASE + "/desde-carrito", json={"nroSuc": 1}).status_code == 409


def test_sucursal_invalida(client, customer):
    llenar(client, [{"idVar": "v1", "cantidad": 1}])
    assert client.post(BASE + "/desde-carrito", json={"nroSuc": 999}).status_code == 404
    assert client.post(BASE + "/desde-carrito", json={"nroSuc": 3}).status_code == 409


def test_sin_disponibilidad_en_sucursal(client, customer, factory):
    # v1 x5 cabe en global (2+5) pero no en sucursal 1 (2).
    assert client.post(CARRITO, json={"idVar": "v1", "cantidad": 2}).status_code == 201
    assert client.post(CARRITO, json={"idVar": "v2", "cantidad": 5}).status_code == 201
    llenar(client, [{"idVar": "v1", "cantidad": 3}])  # total v1 = 5 <= global 7
    assert client.post(BASE + "/desde-carrito", json={"nroSuc": 1}).status_code == 409
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Venta)) == 0


def test_inactivos_en_checkout(client, customer, factory):
    llenar(client, [{"idVar": "v1", "cantidad": 1}])
    with factory.begin() as db:
        db.get(VarianteProd, "v1").estado = "inactivo"
    assert client.post(BASE + "/desde-carrito", json={"nroSuc": 1}).status_code == 404
    with factory.begin() as db:
        db.get(VarianteProd, "v1").estado = "activo"
        db.get(Producto, "p1").estado = "inactivo"
    assert client.post(BASE + "/desde-carrito", json={"nroSuc": 1}).status_code == 404


def test_monto_fijo_y_tope(client, customer):
    llenar(client, [{"idVar": "v3", "cantidad": 1}, {"idVar": "v4", "cantidad": 1}])
    data = client.post(BASE + "/desde-carrito", json={"nroSuc": 1}).json()
    lineas = {i["idVar"]: i for i in data["items"]}
    # Sin descuento por línea persistido: el desglose histórico usa brutos
    # congelados; el descuento vive solo en el total de la venta.
    assert "descuento" not in lineas["v3"] and "descuento" not in lineas["v4"]
    assert str(lineas["v3"]["subtotalBruto"]) in ("200", "200.00")
    assert str(lineas["v4"]["subtotalBruto"]) in ("20", "20.00")
    # Fijo 30 + tope 20 (50 no supera el bruto de 20) solo en totales congelados.
    assert data["brutoTotal"] in (220, "220.00", "220")
    assert data["descAplicado"] in (50, "50.00", "50")
    assert data["total"] in (170, "170.00", "170")


def test_congelamiento_historico(client, customer, factory):
    llenar(client, [{"idVar": "v1", "cantidad": 1}])
    nro = client.post(BASE + "/desde-carrito", json={"nroSuc": 1}).json()["nroVenta"]
    with factory.begin() as db:
        db.get(VarianteProd, "v1").precio = 999
        db.get(Promocion, 1).estado = "inactivo"
    data = client.get(f"{BASE}/{nro}").json()
    assert data["items"][0]["precioUnitario"] in (100, "100.00", "100")
    assert data["descAplicado"] in (10, "10.00", "10")
    assert data["total"] in (90, "90.00", "90")


def test_carrito_activo_e_inventario_intactos(client, customer, factory):
    from app.modules.cliente_experiencia_compra.shared.models.comercio import Carrito
    before = inventario(factory)
    llenar(client, [{"idVar": "v1", "cantidad": 1}])
    data = client.post(BASE + "/desde-carrito", json={"nroSuc": 1}).json()
    with factory() as db:
        assert db.get(Carrito, data["carrito"]).estado == "activo"
    assert inventario(factory) == before
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(MovimientoInv)) == 0


def test_rollback_total(client, customer, factory):
    llenar(client, [{"idVar": "v2", "cantidad": 1}, {"idVar": "v1", "cantidad": 5}])
    assert client.post(BASE + "/desde-carrito", json={"nroSuc": 1}).status_code == 409
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Venta)) == 0
        assert db.scalar(select(func.count()).select_from(DetalleVenta)) == 0


def test_doble_post_idempotente(client, customer, factory):
    llenar(client, [{"idVar": "v1", "cantidad": 1}])
    first = client.post(BASE + "/desde-carrito", json={"nroSuc": 1})
    assert first.status_code == 201
    second = client.post(BASE + "/desde-carrito", json={"nroSuc": 1})
    assert second.status_code == 200
    assert second.json()["nroVenta"] == first.json()["nroVenta"]
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Venta).where(
            Venta.estado == "registrada")) == 1


def test_anulada_permite_reintento(client, customer, factory):
    llenar(client, [{"idVar": "v1", "cantidad": 1}])
    nro = client.post(BASE + "/desde-carrito", json={"nroSuc": 1}).json()["nroVenta"]
    with factory.begin() as db:
        db.get(Venta, nro).estado = "anulada"
    nuevo = client.post(BASE + "/desde-carrito", json={"nroSuc": 1})
    assert nuevo.status_code == 201 and nuevo.json()["nroVenta"] != nro


def test_venta_ajena_404(client, customer, registration):
    llenar(client, [{"idVar": "v1", "cantidad": 1}])
    nro = client.post(BASE + "/desde-carrito", json={"nroSuc": 1}).json()["nroVenta"]
    assert client.post("/api/auth/registro",
                       json=registration | {"correo": "otro@example.com"}).status_code == 201
    assert client.post("/api/auth/login",
                       json={"correo": "otro@example.com",
                             "contrasena": registration["contrasena"]}).status_code == 200
    assert client.get(f"{BASE}/{nro}").status_code == 404


def test_bearer_movil(client, customer, settings):
    token = client.cookies.get(settings.cookie_name)
    client.cookies.clear()
    client.headers.pop("X-CSRF-Protection", None)
    client.headers.pop("Origin", None)
    try:
        llenar_bearer(client, token)
        response = client.post(BASE + "/desde-carrito", json={"nroSuc": 1},
                               headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 201
    finally:
        client.headers.update({"Origin": "http://localhost:4200", "X-CSRF-Protection": "1"})


def llenar_bearer(client, token):
    headers = {"Authorization": f"Bearer {token}"}
    assert client.post(CARRITO, json={"idVar": "v1", "cantidad": 1}, headers=headers).status_code == 201


def test_cookie_mantiene_csrf(client, customer):
    llenar(client, [{"idVar": "v1", "cantidad": 1}])
    client.headers.pop("X-CSRF-Protection", None)
    try:
        assert client.post(BASE + "/desde-carrito", json={"nroSuc": 1}).status_code == 403
    finally:
        client.headers.update({"X-CSRF-Protection": "1"})


def test_cantidad_obligatoria_en_modelo(factory, catalogo):
    from datetime import date as _date
    with factory.begin() as db:
        db.add(Usuario(idusuario="u-ck", ci="1", apellidopat="P", apellidomat="M",
                       sexo="F", correo="ck@example.com", telefono="1", direccion="D",
                       contrasena="x", activo=True, nombres="Ck",
                       fechanac=_date(2000, 1, 1), tipo="C", nrorol="cliente"))
        db.flush()
        db.add(Cliente(idusuario="u-ck", cod_cl="CK1"))
        db.flush()
        db.add(Venta(total=0, desc_aplicado=0, estado="registrada",
                     idusuariocl="u-ck", nrosuc=1))
        db.flush()
        db.add(DetalleVenta(nroventa=1, iddetalleventa=1, preciounitario=10,
                            cantidad=0, idvar="v1"))
        with pytest.raises(IntegrityError):
            db.flush()


def test_nit_opcional(client, customer):
    llenar(client, [{"idVar": "v1", "cantidad": 1}])
    data = client.post(BASE + "/desde-carrito", json={"nroSuc": 1, "nit": "123456-7"}).json()
    assert data["nit"] == "123456-7"
    assert client.post(BASE + "/desde-carrito", json={"nroSuc": 1, "nit": "x" * 31}).status_code == 422


def test_bitacora_venta(client, customer, factory):
    llenar(client, [{"idVar": "v1", "cantidad": 1}])
    assert client.post(BASE + "/desde-carrito", json={"nroSuc": 1}).status_code == 201
    with factory() as db:
        assert db.scalar(select(Bitacora).where(
            Bitacora.accion == "venta_registrada",
            Bitacora.usuario_id == customer)) is not None
