"""Opt-in real PostgreSQL tests. No connections at import/collection time.

Run only after coordination: AUTONOMO_INVENTARIO_POSTGRES=1. All fixtures and
their audit events are uncommitted inside an outer transaction, rolled back at
teardown; no historical audit rows are deleted and no seed/reset is performed.
"""
import os
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core.errors import DomainError
from app.modules.seguridad_accesos.models import Usuario, Rol, RolFuncion
from app.modules.cliente_experiencia_compra.shared.models.catalogo import Producto, VarianteProd, Inventario
from app.modules.inventario_productos.shared.tenant import select_for
from app.modules.inventario_productos.cu18_gestionar_catalogo.repositories import catalogo
from app.modules.inventario_productos.cu20_gestionar_inventario.repositories import inventario as repo
from app.modules.inventario_productos.cu20_gestionar_inventario.services import inventario as service
from app.modules.inventario_productos.cu20_gestionar_inventario.schemas.inventario import MovimientoDatos


def _test_urls():
    values = {}
    path = Path(__file__).resolve().parents[2] / '.local' / 'backend.env'
    if path.exists():
        # Values are used only for connections, never logged or included in assertions.
        for line in path.read_text(encoding='utf-8').splitlines():
            if line.strip() and not line.lstrip().startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                values[key.strip()] = value.strip().strip('\"').strip("'")
    urls = [os.environ.get(key) or values.get(key) for key in ('TEST_DATABASE_URL', 'TEST_PLATFORM_DATABASE_URL')]
    for url, role in zip(urls, ('fashionstore_app', 'fashionstore_provisioner')):
        if not url:
            pytest.fail('Falta URL de PostgreSQL de pruebas autorizada', pytrace=False)
        parsed = make_url(url)
        if (parsed.host, parsed.port, parsed.database, parsed.username) != (
                '127.0.0.1', 55432, 'fashionstore_test', role):
            pytest.fail('Destino PostgreSQL no autorizado', pytrace=False)
    return urls


def _validate_connection(connection, role):
    actual = connection.execute(text('SELECT current_database(), current_user, inet_server_port()')).one()
    if tuple(actual) != ('fashionstore_test', role, 55432):
        pytest.fail('Identidad del servidor PostgreSQL no autorizada', pytrace=False)


@pytest.fixture
def pg_inventory():
    if os.environ.get('AUTONOMO_INVENTARIO_POSTGRES') != '1':
        pytest.skip('Ejecución PostgreSQL requiere coordinación y opt-in explícito')
    app_url, platform_url = _test_urls()
    platform = create_engine(platform_url, hide_parameters=True)
    app = create_engine(app_url, hide_parameters=True)
    try:
        with platform.connect() as connection:
            _validate_connection(connection, 'fashionstore_provisioner')
            candidates = connection.execute(text('''
                SELECT DISTINCT ON (p.idemp) p.idemp, p.idprod, v.idvariante, s.nro
                FROM producto p JOIN varianteprod v ON v.idemp=p.idemp AND v.idprod=p.idprod
                JOIN sucursal s ON s.idemp=p.idemp AND s.estado='activo'
                ORDER BY p.idemp, p.idprod, v.idvariante, s.nro LIMIT 2
            ''')).all()
        assert len(candidates) == 2, 'Se requieren dos empresas con catálogo y sucursal existentes; no se crea seed'
        tenant, source_product, source_variant, branch = candidates[0]
        other = candidates[1]
        with app.connect() as connection:
            outer = connection.begin()
            try:
                _validate_connection(connection, 'fashionstore_app')
                with Session(bind=connection, expire_on_commit=False, join_transaction_mode='create_savepoint') as db:
                    db.info.update(multitenant=True, idemp=tenant)
                    token = uuid4().hex[:10]
                    actor = 'AINV' + token
                    role = 'AIR' + token
                    db.add(Rol(nro=role, descripcion='Fixture inventario', estado='activo', idemp=tenant))
                    db.flush()
                    db.add(RolFuncion(nrorol=role, idfun='CU20', descripcion='Fixture inventario', idemp=tenant))
                    db.add(Usuario(idusuario=actor, ci=token, nombre='Fixture inventario', apellidopat='Prueba',
                                   apellidomat='Prueba', sexo='F', correo=actor + '@example.test', telefono='12345678',
                                   direccion='Dirección de prueba', contrasena='not-an-authentication-fixture',
                                   fechanac=date(2000, 1, 1), tipo='A', nrorol=role, idemp=tenant))
                    source = catalogo.get(db, Producto, source_product)
                    variant_source = catalogo.get(db, VarianteProd, source_variant)
                    product_id, variant_id = 'AINVP' + token, 'AINV' + token
                    db.add(Producto(idprod=product_id, descripcion='Fixture inventario', estado='activo',
                                    idcat=source.idcat, idmarca=source.idmarca, idcol=source.idcol,
                                    idprov=source.idprov, idemp=tenant))
                    db.flush()
                    db.add(VarianteProd(idvariante=variant_id, sku='AINV-' + token, precio=20, estado='activo',
                                        idtalla=variant_source.idtalla, idprod=product_id, idemp=tenant))
                    db.commit()  # Releases only a savepoint, never the outer transaction.
                    yield db, actor, tenant, branch, variant_id, other
            finally:
                outer.rollback()
    finally:
        app.dispose()
        platform.dispose()


def _movement(branch, variant, kind, count):
    return MovimientoDatos(nroSuc=branch, idVariante=variant, tipoMov=kind,
                           cantidad=count, motivo='Fixture autónomo inventario')


def test_postgres_movement_procedure_history_and_atomic_failure(pg_inventory):
    db, actor, tenant, branch, variant, _ = pg_inventory
    try:
        result = service.register(db, _movement(branch, variant, 'entrada', 5), actor, None)
    except DomainError as error:
        original = getattr(error.__context__, 'orig', None)
        constraint = getattr(getattr(original, 'diag', None), 'constraint_name', None)
        raise AssertionError(f'Movimiento rechazado: {error.code}; restricción={constraint}') from None
    assert (result['stock'], result['cantDisp']) == (5, 5)
    inventory_id = result['nroInv']
    # Simulates two reserved units only on our fixture, without editing existing stock.
    own = catalogo.get(db, Inventario, inventory_id, lock=True)
    own.cantdisp = 3
    db.commit()
    result = service.register(db, _movement(branch, variant, 'salida', 3), actor, None)
    assert (result['stock'], result['cantDisp']) == (2, 0)
    count_before = db.scalar(text('SELECT count(*) FROM bitacora WHERE idemp=:tenant AND usuario_id=:actor'),
                             {'tenant': tenant, 'actor': actor})
    with pytest.raises(DomainError) as error:
        service.register(db, _movement(branch, variant, 'salida', 1), actor, None)
    assert error.value.code == 'stock_insuficiente'
    assert repo.history(db, inventory_id, 0, 20)['total'] == 2
    assert db.scalar(text('SELECT count(*) FROM movimiento_inv WHERE idemp=:tenant AND nroinv=:id'),
                     {'tenant': tenant, 'id': inventory_id}) == 2
    assert db.scalar(text('SELECT count(*) FROM bitacora WHERE idemp=:tenant AND usuario_id=:actor'),
                     {'tenant': tenant, 'actor': actor}) == count_before


def test_postgres_foreign_fks_and_admin_reads(pg_inventory):
    db, actor, tenant, branch, variant, other = pg_inventory
    _, other_product, other_variant, other_branch = other
    for selected_branch, selected_variant in ((branch, other_variant), (other_branch, variant)):
        with pytest.raises(DomainError) as error:
            service.register(db, _movement(selected_branch, selected_variant, 'entrada', 1), actor, None)
        assert error.value.status == 422
    with pytest.raises(DomainError) as error:
        catalogo.get(db, Producto, other_product)
    assert error.value.status == 404
    assert catalogo.list_products(db, other_product, None, 0, 20)['total'] == 0
    references = catalogo.references(db)
    assert references['proveedores']
    assert db.scalar(select_for(db, Inventario.nroinv).where(Inventario.idvar == variant)) is None
