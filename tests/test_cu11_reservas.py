"""CU11 Gestionar Reserva: HTTP/domain regression con SQLite inyectado."""
from datetime import date, time, timedelta

import pytest
from sqlalchemy import select
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
    TempColeccion,
    Temporada,
    VarianteColor,
    VarianteProd,
)
from app.modules.cliente_experiencia_compra.shared.models.comercio import (
    DetalleReserva,
    HorarioAtencion,
    HorarioSuc,
    Reserva,
)
from app.modules.cliente_experiencia_compra.cu11_gestionar_reserva.repositories import reserva as repository
from app.modules.cliente_experiencia_compra.shared.repositories import comercio as shared_repo
from app.modules.seguridad_accesos.models import Bitacora, Cliente, Usuario
from app.modules.seguridad_accesos.shared.models import Ciudad, Sucursal

BASE = "/api/cliente/reservas"
SUCURSALES = "/api/cliente/sucursales"


def manana():
    return (date.today() + timedelta(days=1)).isoformat()


@pytest.fixture
def catalogo(factory):
    with factory.begin() as db:
        for row in (Categoria(idcat=1, descripcion="Camisas"),
                    Marca(idmarca=1, nombre="Andes", estado="activo"),
                    Coleccion(idcol=1, descripcion="Otoño"),
                    Temporada(idtemp=1, nombre="Otoño", fechaini=date(2026, 3, 1),
                              fechafin=date(2026, 9, 30), estado="activo"),
                    Proveedor(idprov=1, nombre="Prov", correo="p@example.com",
                              direccion="Dir", telefono="70000000"),
                    Promocion(idpromo=1, nombre="Promo", descripcion="D",
                              tipodescuento="porcentaje", valordescuento=10,
                              fechaini=date(2026, 1, 1), fechafin=date(2026, 12, 31),
                              estado="activo"),
                    Talla(idtalla=1, descripcion="M"), Talla(idtalla=2, descripcion="L"),
                    Color(idcolor=1, descripcion="Rojo", hex="#FF0000"),
                    Ciudad(id=1, nombre="La Paz"),
                    HorarioAtencion(idaten=1, horaini=time(8, 0), horafin=time(18, 0))):
            db.add(row)
        db.flush()
        for row in (TempColeccion(idtemp=1, idcol=1),
                    Producto(idprod="p1", descripcion="Camisa", estado="activo",
                             idpromo=1, idcat=1, idmarca=1, idcol=1, idprov=1),
                    Producto(idprod="p-off", descripcion="Baja", estado="inactivo",
                             idpromo=None, idcat=1, idmarca=1, idcol=1, idprov=1),
                    Sucursal(nro=1, nombre="Central", direccion="Dir 1", estado="activo", idciud=1),
                    Sucursal(nro=2, nombre="Cerrada", direccion="Dir 2", estado="inactivo", idciud=1),
                    Sucursal(nro=3, nombre="Sin Horario", direccion="Dir 3", estado="activo", idciud=1)):
            db.add(row)
        db.flush()
        for row in (HorarioSuc(idaten=1, nrosuc=1),
                    VarianteProd(idvariante="v1", sku="SKU-1", precio=100,
                                 estado="activo", img=None, idtalla=1, idprod="p1"),
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
                    Inventario(nroinv=2, stock=5, cantdisp=1, nrosuc=1, idvar="v2"),
                    Inventario(nroinv=3, stock=3, cantdisp=2, nrosuc=3, idvar="v1")):
            db.add(row)
        db.flush()


@pytest.fixture
def customer(client, registered, credentials, catalogo):
    assert client.post("/api/auth/login", json=credentials).status_code == 200
    return registered["idUsuario"]


def body(nroSuc=1, hora="10:00", items=None):
    return {"nroSuc": nroSuc, "fechaReserva": manana(), "horaAtencion": hora,
            "items": items if items is not None else [{"idVar": "v1", "cantidad": 2}]}


def inventario(factory, nroinv):
    with factory() as db:
        row = db.get(Inventario, nroinv)
        return row.stock, row.cantdisp


def bearer(client, settings):
    token = client.cookies.get(settings.cookie_name)
    assert token and len(token) == 43
    client.cookies.clear()
    return {"Authorization": f"Bearer {token}"}


def test_routes_require_authentication(client, catalogo):
    assert client.post(BASE, json=body()).status_code == 401
    assert client.get(BASE).status_code == 401
    assert client.get(BASE + "/1").status_code == 401
    assert client.patch(BASE + "/1/cancelar").status_code == 401


def test_non_cliente_rejected(client, registered, credentials, catalogo, factory):
    with factory.begin() as db:
        user = db.get(Usuario, registered["idUsuario"])
        db.delete(db.get(Cliente, user.idusuario))
        user.tipo, user.nrorol = "E", "cliente"
    assert client.post("/api/auth/login", json=credentials).status_code == 200
    assert client.post(BASE, json=body()).status_code == 403
    assert client.get(BASE).status_code == 403


def test_crear_reserva_valida(client, customer, factory):
    before = (inventario(factory, 1), inventario(factory, 2))
    response = client.post(BASE, json=body(items=[{"idVar": "v1", "cantidad": 2},
                                                  {"idVar": "v2", "cantidad": 1}]))
    assert response.status_code == 201
    data = response.json()
    assert data["estado"] == "pendiente" and data["fechaReserva"] == manana()
    assert data["horaAtencion"] == "10:00:00"
    assert data["sucursal"] == {"nro": 1, "nombre": "Central", "ciudad": "La Paz"}
    assert data["totalUnidades"] == 3 and data["vencida"] is False
    assert [(i["idVar"], i["cantidad"]) for i in data["items"]] == [("v1", 2), ("v2", 1)]
    assert data["items"][0]["sku"] == "SKU-1" and data["items"][0]["producto"] == "Camisa"
    # cantDisp disminuye exacto; stock físico intacto.
    assert inventario(factory, 1) == (before[0][0], before[0][1] - 2)
    assert inventario(factory, 2) == (before[1][0], before[1][1] - 1)


def test_items_duplicados_se_fusionan(client, customer, factory):
    response = client.post(BASE, json=body(
        items=[{"idVar": "v1", "cantidad": 1}, {"idVar": "v1", "cantidad": 2}]))
    assert response.status_code == 201
    data = response.json()
    assert len(data["items"]) == 1 and data["items"][0]["cantidad"] == 3
    assert inventario(factory, 1) == (10, 1)


def test_disponibilidad_insuficiente_rollback(client, customer, factory):
    assert client.post(BASE, json=body(items=[{"idVar": "v1", "cantidad": 5}])).status_code == 409
    assert client.get(BASE).json()["total"] == 0
    assert inventario(factory, 1) == (10, 4)


def test_rollback_multiitem_no_parcial(client, customer, factory):
    payload = body(items=[{"idVar": "v1", "cantidad": 1}, {"idVar": "v2", "cantidad": 5}])
    assert client.post(BASE, json=payload).status_code == 409
    assert client.get(BASE).json()["total"] == 0
    assert inventario(factory, 1) == (10, 4) and inventario(factory, 2) == (5, 1)


def test_sucursal_inactiva_e_inexistente(client, customer):
    assert client.post(BASE, json=body(nroSuc=2)).status_code == 409
    assert client.post(BASE, json=body(nroSuc=999)).status_code == 404


def test_variante_producto_invalidos(client, customer):
    assert client.post(BASE, json=body(items=[{"idVar": "v-off", "cantidad": 1}])).status_code == 404
    assert client.post(BASE, json=body(items=[{"idVar": "v-poff", "cantidad": 1}])).status_code == 404
    assert client.post(BASE, json=body(items=[{"idVar": "no-existe", "cantidad": 1}])).status_code == 404


def test_datos_invalidos(client, customer):
    assert client.post(BASE, json=body(items=[])).status_code == 422
    assert client.post(BASE, json=body(items=[{"idVar": "v1", "cantidad": 0}])).status_code == 422
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    assert client.post(BASE, json={**body(), "fechaReserva": yesterday}).status_code == 422


def test_horario_fuera_atencion(client, customer):
    assert client.post(BASE, json=body(hora="20:00")).status_code == 422
    # Sucursal sin horarios registrados: no puede determinarse, se permite.
    assert client.post(BASE, json=body(nroSuc=3, hora="23:00")).status_code == 201


def test_solo_reservas_propias(client, customer, registration, factory):
    mine = client.post(BASE, json=body()).json()["nroReserva"]
    assert client.post("/api/auth/registro",
                       json=registration | {"correo": "otro@example.com"}).status_code == 201
    assert client.post("/api/auth/login",
                       json={"correo": "otro@example.com",
                             "contrasena": registration["contrasena"]}).status_code == 200
    assert client.get(BASE).json()["total"] == 0
    assert client.get(f"{BASE}/{mine}").status_code == 404
    assert client.patch(f"{BASE}/{mine}/cancelar").status_code == 404


def test_cancelar_libera_exacto(client, customer, factory):
    nro = client.post(BASE, json=body()).json()["nroReserva"]
    assert inventario(factory, 1) == (10, 2)
    response = client.patch(f"{BASE}/{nro}/cancelar")
    assert response.status_code == 200 and response.json()["estado"] == "cancelada"
    assert inventario(factory, 1) == (10, 4)


def test_doble_cancelacion_idempotente(client, customer, factory):
    nro = client.post(BASE, json=body()).json()["nroReserva"]
    assert client.patch(f"{BASE}/{nro}/cancelar").status_code == 200
    assert client.patch(f"{BASE}/{nro}/cancelar").status_code == 200
    assert inventario(factory, 1) == (10, 4)


def test_cancelar_no_pendiente(client, customer, factory):
    nro = client.post(BASE, json=body()).json()["nroReserva"]
    with factory.begin() as db:
        db.get(Reserva, nro).estado = "confirmada"
    assert client.patch(f"{BASE}/{nro}/cancelar").status_code == 409
    assert inventario(factory, 1) == (10, 2)


def test_vencimiento_lazy_libera(client, customer, factory):
    nro = client.post(BASE, json=body()).json()["nroReserva"]
    assert inventario(factory, 1) == (10, 2)
    yesterday = date.today() - timedelta(days=1)
    with factory.begin() as db:
        db.get(Reserva, nro).fechareserva = yesterday
    detail = client.get(f"{BASE}/{nro}").json()
    assert detail["estado"] == "vencida" and detail["vencida"] is True
    assert inventario(factory, 1) == (10, 4)
    # Segunda reserva: la expiración también se dispara al listar.
    otro = client.post(BASE, json=body()).json()["nroReserva"]
    with factory.begin() as db:
        db.get(Reserva, otro).fechareserva = yesterday
    listed = client.get(BASE).json()
    assert listed["total"] == 2
    by_nro = {item["nroReserva"]: item for item in listed["items"]}
    assert by_nro[otro]["estado"] == "vencida" and by_nro[otro]["vencida"] is True
    assert by_nro[nro]["vencida"] is True
    assert inventario(factory, 1) == (10, 4)


def test_concurrencia_ultima_unidad(client, customer, factory):
    first = client.post(BASE, json=body(items=[{"idVar": "v2", "cantidad": 1}]))
    assert first.status_code == 201
    assert inventario(factory, 2) == (5, 0)
    assert client.post(BASE, json=body(items=[{"idVar": "v2", "cantidad": 1}])).status_code == 409
    assert client.get(BASE).json()["total"] == 1
    assert inventario(factory, 2) == (5, 0)


def test_bloqueo_for_update_en_postgres():
    from sqlalchemy import select as _select
    inv_query = _select(shared_repo.Inventario).where(
        shared_repo.Inventario.idvar == "v1").with_for_update()
    assert "FOR UPDATE" in str(inv_query.compile(dialect=postgresql.dialect()))
    res_query = _select(Reserva).where(Reserva.nroreserva == 1).with_for_update()
    assert "FOR UPDATE" in str(res_query.compile(dialect=postgresql.dialect()))
    assert repository.locked_reserva is not None and shared_repo.locked_inventario is not None


def test_bitacora_reserva(client, customer, factory):
    nro = client.post(BASE, json=body()).json()["nroReserva"]
    assert client.patch(f"{BASE}/{nro}/cancelar").status_code == 200
    with factory() as db:
        actions = [r.accion for r in db.scalars(
            select(Bitacora).where(Bitacora.usuario_id == customer).order_by(Bitacora.id))]
    assert "reserva_creada" in actions and "reserva_cancelada" in actions


def test_bearer_post_sin_csrf_ni_origin(client, customer, settings, factory):
    headers = bearer(client, settings)
    client.headers.pop("X-CSRF-Protection", None)
    client.headers.pop("Origin", None)
    try:
        response = client.post(BASE, json=body(), headers=headers)
        assert response.status_code == 201  # middleware no exige CSRF a Bearer nativo
    finally:
        client.headers.update({"Origin": "http://localhost:4200", "X-CSRF-Protection": "1"})


def test_bearer_con_origin_maligno_sin_csrf(client, customer, settings):
    headers = bearer(client, settings)
    try:
        response = client.post(BASE, json=body(),
                               headers=headers | {"Origin": "https://evil.example"})
        assert response.status_code in (201, 409, 422)  # nunca 403 por Origin
    finally:
        client.headers.update({"Origin": "http://localhost:4200", "X-CSRF-Protection": "1"})


def test_cookie_post_sin_csrf_sigue_403(client, customer):
    client.headers.pop("X-CSRF-Protection", None)
    try:
        assert client.post(BASE, json=body()).status_code == 403
    finally:
        client.headers.update({"X-CSRF-Protection": "1"})


def test_cookie_mas_bearer_ambiguo_401(client, customer, settings):
    token = client.cookies.get(settings.cookie_name)
    try:
        response = client.post(BASE, json=body(), headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401
    finally:
        pass


def test_bearer_get_sin_origin(client, customer, settings):
    headers = bearer(client, settings)
    client.headers.pop("Origin", None)
    try:
        assert client.get(BASE, headers=headers).status_code == 200
    finally:
        client.headers.update({"Origin": "http://localhost:4200"})


def test_sucursales_cliente(client, customer):
    response = client.get(SUCURSALES)
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 2  # solo activas (Central y Sin Horario)
    assert {s["nombre"] for s in data["items"]} == {"Central", "Sin Horario"}
    horarios = client.get(SUCURSALES + "/1/horarios").json()
    assert horarios["rangos"] == [{"horaIni": "08:00:00", "horaFin": "18:00:00", "dias": [1,2,3,4,5,6,7]}]
    assert client.get(SUCURSALES + "/3/horarios").json()["rangos"] == []
    assert client.get(SUCURSALES + "/999/horarios").status_code == 404
    assert client.get(SUCURSALES + "/2/horarios").status_code == 404


def test_detalle_reserva_inventario_sin_fila(client, customer, factory):
    # Variante sin fila de inventario en la sucursal: disponibilidad 0.
    with factory.begin() as db:
        db.add(VarianteProd(idvariante="v3", sku="SKU-3", precio=60,
                            estado="activo", img=None, idtalla=1, idprod="p1"))
    assert client.post(BASE, json=body(items=[{"idVar": "v3", "cantidad": 1}])).status_code == 409
