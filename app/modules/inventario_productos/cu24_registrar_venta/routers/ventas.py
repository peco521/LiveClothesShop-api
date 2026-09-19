from fastapi import APIRouter, Depends, Query, Request, Response
from app.core.database import get_db
from app.modules.inventario_productos.shared.operaciones_access import access
from app.modules.inventario_productos.shared.operaciones_referencias import add_references, peer
from app.modules.inventario_productos.cu24_registrar_venta.services import ventas
from app.modules.inventario_productos.cu24_registrar_venta.schemas.ventas import SaleInput, CashInput, ElectronicInput
from app.modules.seguridad_accesos.cu07_clientes.schemas.cliente import ClienteCrear
from sqlalchemy import select
from app.modules.cliente_experiencia_compra.cu14_pago_electronico.services import checkout_stripe
from app.modules.inventario_productos.shared.access import transaction
from app.modules.seguridad_accesos.services.bitacora import record

router = APIRouter(prefix='/api/admin/caja', tags=['CU24'])
a24 = access('CU24')
add_references(router, a24)


@router.get('/clientes')
def cash_customers(q: str = Query('', max_length=100), idUsuario: str | None = Query(None, max_length=100),
                   db=Depends(get_db), actor=Depends(a24)):
    return ventas.customers(db, q, idUsuario)


@router.post('/clientes', status_code=201)
def register_cash_customer(data: ClienteCrear, request: Request, db=Depends(get_db), actor=Depends(a24)):
    # CU24: registra al cliente para esta venta sin cambiar la sesión de caja.
    return ventas.register_client(db, data, request.app.state.settings,
                                  request.app.state.passwords, actor[0], peer(request))


@router.post('', status_code=201)
def sale_create(data: SaleInput, request: Request, db=Depends(get_db), actor=Depends(a24)):
    return ventas.create(db, data, actor[0], actor[1], peer(request))


@router.get('/{nro}')
def sale_detail(nro: int, request: Request, db=Depends(get_db), actor=Depends(a24)):
    return ventas.view(db, ventas.get(db, nro, actor[1]))


@router.post('/{nro}/efectivo')
def sale_cash(nro: int, data: CashInput, request: Request, db=Depends(get_db), actor=Depends(a24)):
    return ventas.cash(db, nro, data.recibido, actor[0], actor[1], peer(request))


@router.post('/{nro}/electronico')
def sale_electronic(nro: int, data: ElectronicInput, request: Request, db=Depends(get_db), actor=Depends(a24)):
    return ventas.electronic(db, nro, data.metodo, actor[0], actor[1], peer(request), request.app)


@router.post('/{nro}/consultar-pago')
def sale_reconcile(nro: int, request: Request, db=Depends(get_db), actor=Depends(a24)):
    from app.modules.cliente_experiencia_compra.shared.models.comercio import Pago
    row = ventas.get(db, nro, actor[1]); owner = row.idusuariocl
    payment = db.scalar(select(Pago).where(Pago.nroventa == nro, Pago.estado == 'pendiente'))
    id_pago = payment.idpago if payment else None
    db.commit()
    if id_pago and request.app.state.stripe:
        checkout_stripe.reconcile(db, id_pago, owner, peer(request), request.app.state.stripe)
    return ventas.view(db, ventas.get(db, nro, actor[1]))


@router.post('/{nro}/cancelar')
def sale_cancel(nro: int, request: Request, db=Depends(get_db), actor=Depends(a24)):
    row = ventas.get(db, nro, actor[1]); owner = row.idusuariocl
    db.commit()
    result = checkout_stripe.cancel_sale(db, nro, owner, peer(request), request.app.state.stripe)
    with transaction(db): record(db, 'venta_caja_cancelada', actor[0], peer(request), True)
    return result
