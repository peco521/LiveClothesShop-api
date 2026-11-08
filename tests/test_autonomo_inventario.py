"""Administrative inventory contract; SQLite does not demonstrate PostgreSQL/RLS."""
from datetime import date
from types import SimpleNamespace
from contextlib import nullcontext

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.core.database import get_db
from app.core.dependencies import current_identity
from app.core.errors import DomainError, register_handlers
from app.modules.seguridad_accesos.models import Usuario, Rol, RolFuncion, Funcion
from app.modules.seguridad_accesos.cu05_usuarios_empleados.models.empleado import Empleado
from app.modules.seguridad_accesos.shared.models import Ciudad, Sucursal
from app.modules.seguridad_accesos.models.bitacora import Bitacora
from app.modules.cliente_experiencia_compra.shared.models.catalogo import (
    Categoria, Marca, Coleccion, Proveedor, Talla, Color, Producto, VarianteProd,
    VarianteColor, Inventario, Promocion,
)
from app.modules.inventario_productos.cu18_gestionar_catalogo.routers.catalogo import router as catalog_router
from app.modules.inventario_productos.cu19_gestionar_proveedores.routers.proveedor import router as supplier_router
from app.modules.inventario_productos.cu20_gestionar_inventario.routers.inventario import router as inventory_router
from app.modules.inventario_productos.CU21_gestionar_promo_desc.routers.promociones import router as promo_router
from app.modules.inventario_productos.cu18_gestionar_catalogo.repositories import catalogo
from app.modules.inventario_productos.cu20_gestionar_inventario.repositories import inventario as repo
from app.modules.inventario_productos.cu20_gestionar_inventario.services import inventario as service
from app.modules.inventario_productos.cu20_gestionar_inventario.schemas.inventario import MovimientoDatos
from app.modules.inventario_productos.shared.access import actor_scope


@pytest.fixture
def inventory_db(factory):
    with factory() as db:
        for permission in ('CU18', 'CU19', 'CU20', 'CU21'):
            db.add(Funcion(id=permission, descripcion=permission))
        db.flush()
        for tenant in (1, 2):
            db.add_all([
                Rol(nro=f'AI{tenant}', descripcion='Inventario', estado='activo', idemp=tenant),
                Ciudad(id=tenant, nombre=f'Ciudad {tenant}', idemp=tenant),
                Categoria(idcat=tenant, descripcion=f'Categoria {tenant}', idemp=tenant),
                Marca(idmarca=tenant, nombre=f'Marca {tenant}', estado='activo', idemp=tenant),
                Coleccion(idcol=tenant, descripcion=f'Coleccion {tenant}', idemp=tenant),
                Proveedor(idprov=tenant, nombre=f'Proveedor {tenant}', correo=f'p{tenant}@example.test',
                          direccion='Dirección', telefono='12345678', idemp=tenant),
                Talla(idtalla=tenant, descripcion=f'T{tenant}', idemp=tenant),
                Color(idcolor=tenant, descripcion=f'Color {tenant}', hex='#ffffff', idemp=tenant),
                Promocion(idpromo=tenant, nombre=f'Promo {tenant}', descripcion='Prueba',
                          tipodescuento='porcentaje', valordescuento=10, fechaini=date(2020, 1, 1),
                          fechafin=date(2099, 1, 1), estado='activo', idemp=tenant),
            ])
            db.flush()
            db.add_all([RolFuncion(nrorol=f'AI{tenant}', idfun=p, descripcion=p, idemp=tenant)
                        for p in ('CU18', 'CU19', 'CU20', 'CU21')])
            db.add(Sucursal(nro=tenant, nombre=f'Sucursal {tenant}', direccion='Dirección',
                            idciud=tenant, estado='activo', idemp=tenant))
            db.add(Usuario(idusuario=f'AI{tenant}', ci=str(tenant), nombre='Prueba', apellidopat='Prueba',
                           apellidomat='Prueba', sexo='F', correo=f'ai{tenant}@example.test', telefono='12345678',
                           direccion='Dirección', contrasena='not-an-authentication-fixture', fechanac=date(2000, 1, 1),
                           tipo='A', nrorol=f'AI{tenant}', idemp=tenant))
            db.add(Producto(idprod=f'AIP{tenant}', descripcion=f'Producto {tenant}', estado='activo',
                            idcat=tenant, idmarca=tenant, idcol=tenant, idprov=tenant, idpromo=tenant, idemp=tenant))
            db.flush()
            db.add(VarianteProd(idvariante=f'AIV{tenant}', sku=f'AISKU{tenant}', precio=20,
                                estado='activo', idtalla=tenant, idprod=f'AIP{tenant}', idemp=tenant))
            db.flush()
            db.add(VarianteColor(idvar=f'AIV{tenant}', idcolor=tenant, idemp=tenant))
            db.add(Inventario(nroinv=tenant, nrosuc=tenant, idvar=f'AIV{tenant}', stock=10, cantdisp=7, idemp=tenant))
        db.commit()
        db.info.update(multitenant=True, idemp=1)
        yield db


@pytest.fixture
def admin_client(inventory_db):
    app = FastAPI()
    register_handlers(app)
    for router in (catalog_router, supplier_router, inventory_router, promo_router):
        app.include_router(router)
    identity = SimpleNamespace(usuario=SimpleNamespace(idUsuario='AI1'), permisos=['CU18', 'CU19', 'CU20', 'CU21'])
    app.dependency_overrides[get_db] = lambda: inventory_db
    app.dependency_overrides[current_identity] = lambda: identity
    with TestClient(app) as client:
        yield client, identity


@pytest.mark.parametrize('path,key', [
    ('/api/admin/catalogo/productos', 'idProd'), ('/api/admin/catalogo/categorias', 'id'),
    ('/api/admin/proveedores', 'id'), ('/api/admin/promociones', 'idPromo'),
    ('/api/admin/inventario', 'nroInv'),
])
def test_admin_lists_counts_are_tenant_scoped(admin_client, path, key):
    client, _ = admin_client
    response = client.get(path)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['total'] == 1
    assert [r[key] for r in result['items']] == (['AIP1'] if key == 'idProd' else [1])


def test_references_details_and_search_are_isolated(admin_client):
    client, _ = admin_client
    for path in ('/api/admin/catalogo/referencias', '/api/admin/inventario/referencias', '/api/admin/promociones/referencias'):
        response = client.get(path)
        assert response.status_code == 200, response.text
        assert 'Sucursal 2' not in response.text
        assert 'Proveedor 2' not in response.text
        assert 'Categoria 2' not in response.text
    for path in ('/api/admin/catalogo/productos/AIP2', '/api/admin/proveedores/2', '/api/admin/inventario/2/movimientos'):
        assert client.get(path).status_code == 404
    assert client.get('/api/admin/catalogo/productos?q=AISKU2').json()['total'] == 0
    assert client.get('/api/admin/promociones').json()['items'][0]['productos'] == ['AIP1']


def test_permissions_and_tenant_selectors(admin_client, inventory_db):
    client, identity = admin_client
    identity.permisos = []
    assert client.get('/api/admin/inventario').status_code == 403
    identity.permisos = ['CU20']
    for suffix in ('?idEmp=2', '?tenant_id=2'):
        assert client.get('/api/admin/inventario' + suffix).status_code == 422
    assert client.get('/api/admin/inventario', headers={'X-Tenant-Id': '2'}).status_code == 422
    with pytest.raises(DomainError) as error:
        actor_scope(inventory_db, 'AI2', 'CU20', True)
    assert error.value.status == 401
    inventory_db.info.pop('idemp')
    assert client.get('/api/admin/inventario').status_code == 403


def test_movements_round_trip_reservations_and_atomic_audit(admin_client, inventory_db):
    client, _ = admin_client
    payload = dict(nroSuc=1, idVariante='AIV1', tipoMov='entrada', cantidad=5, motivo='Entrada de prueba')
    initial_audit = inventory_db.scalar(select(func.count()).select_from(Bitacora))
    result = client.post('/api/admin/inventario/movimientos', json=payload)
    assert result.status_code == 201, result.text
    assert (result.json()['stock'], result.json()['cantDisp']) == (15, 12)
    payload.update(tipoMov='salida', cantidad=12)
    result = client.post('/api/admin/inventario/movimientos', json=payload)
    assert result.status_code == 201
    assert (result.json()['stock'], result.json()['cantDisp']) == (3, 0)
    payload['cantidad'] = 1
    assert client.post('/api/admin/inventario/movimientos', json=payload).status_code == 409
    history = client.get('/api/admin/inventario/1/movimientos').json()
    assert history['total'] == 2
    assert inventory_db.scalar(select(func.count()).select_from(Bitacora)) == initial_audit + 2
    assert client.get('/api/admin/inventario').json()['items'][0]['reservado'] == 3


def test_foreign_references_and_branch_scope_fail_before_mutation(admin_client, inventory_db):
    client, _ = admin_client
    payload = dict(nroSuc=1, idVariante='AIV2', tipoMov='entrada', cantidad=1, motivo='Prueba ajena')
    assert client.post('/api/admin/inventario/movimientos', json=payload).status_code == 422
    payload.update(nroSuc=2, idVariante='AIV1')
    assert client.post('/api/admin/inventario/movimientos', json=payload).status_code == 422
    user = inventory_db.get(Usuario, 'AI1')
    user.tipo = 'E'
    inventory_db.add(Empleado(idusuario='AI1', cod_emp='AI1', cargo='Encargada', nrosuc=1, estado='activo', idemp=1))
    inventory_db.commit()
    assert client.get('/api/admin/inventario?nroSuc=2').status_code == 403
    assert client.post('/api/admin/inventario/movimientos', json=payload).status_code == 403


def test_only_verified_saas_writes_are_available(admin_client):
    client, _ = admin_client
    # Suppliers are now enabled, but referenced history must still be preserved.
    assert client.delete('/api/admin/proveedores/1').status_code == 409
    assert client.delete('/api/admin/catalogo/productos/AIP1').status_code == 409
    assert client.post('/api/admin/promociones/1/desactivar').status_code == 200
    payload = dict(nroSuc=1, idVariante='AIV1', tipoMov='ajuste', ajusteDireccion='disminuir', cantidad=1, motivo='Prueba ajuste')
    assert client.post('/api/admin/inventario/movimientos', json=payload).status_code == 422


def test_new_inventory_uses_session_tenant(inventory_db):
    inventory_db.add(VarianteProd(idvariante='AIVNEW', sku='AINEW', precio=10, estado='activo',
                                    idtalla=1, idprod='AIP1', idemp=1))
    inventory_db.commit()
    result = service.register(inventory_db, MovimientoDatos(nroSuc=1, idVariante='AIVNEW', tipoMov='entrada',
                                                          cantidad=2, motivo='Nueva existencia'), 'AI1', None)
    row = catalogo.get(inventory_db, Inventario, result['nroInv'])
    assert row.idemp == 1
    assert (row.stock, row.cantdisp) == (2, 2)


def test_saas_procedure_signature_has_explicit_tenant():
    class Capture:
        info = {'multitenant': True, 'idemp': 7}
        def execute(self, statement, params):
            self.statement, self.params = str(statement), params
        def begin_nested(self): return nullcontext()
    db = Capture()
    repo.call_movement(db, 9, 'entrada', 2, 'Prueba')
    assert '(:tenant, :id,' in db.statement
    assert db.params['tenant'] == 7
