from datetime import datetime, timedelta
from decimal import Decimal
from sqlalchemy import func, select
from app.core.database import is_postgresql
from app.core.errors import DomainError
from app.modules.inventario_productos.cu23_gestionar_devoluciones.models.devoluciones import PoliticaDevolucion, Devolucion, DetalleDev, Reembolso
from app.modules.inventario_productos.cu23_gestionar_devoluciones.repositories import devoluciones as repo
from app.modules.inventario_productos.shared.operaciones_access import branch
from app.modules.inventario_productos.shared.access import transaction
from app.modules.cliente_experiencia_compra.shared.models.comercio import Venta, Pago, DetalleVenta, MovimientoInv
from app.modules.cliente_experiencia_compra.shared.models.catalogo import Producto, VarianteProd, Inventario
from app.modules.cliente_experiencia_compra.shared.repositories import comercio
from app.modules.cliente_experiencia_compra.shared.services.precios import moneda
from app.modules.cliente_experiencia_compra.cu13_compra_digital.services.compra import _vista
from app.modules.seguridad_accesos.services.bitacora import record


def policy_view(row, db=None):
    result = dict(id=row.id, idProd=row.idprod, idVar=row.idvar, dias=row.dias, porcentaje=row.porcentaje)
    if db is not None:
        from app.modules.cliente_experiencia_compra.shared.repositories import catalogo
        product = db.get(Producto, row.idprod)
        variant = db.get(VarianteProd, row.idvar) if row.idvar else None
        result['producto'] = product.descripcion if product else 'Prenda no disponible'
        if variant:
            talla = catalogo.tallas_map(db, [variant.idtalla]).get(variant.idtalla)
            colors = catalogo.variant_colors(db, [variant.idvariante]).get(variant.idvariante, [])
            result['opcion'] = 'Talla ' + (talla.descripcion if talla else '') + ' · ' + ', '.join(c.descripcion for c in colors)
        else:
            result['opcion'] = 'Todo el producto'
    return result


def policy_save(db, data, actor, peer):
    with transaction(db):
        product = db.scalar(select(Producto).where(Producto.idprod == data.idProd).with_for_update())
        if not product:
            raise DomainError(404, 'producto_no_encontrado', 'Producto no encontrado')
        variant = db.get(VarianteProd, data.idVar) if data.idVar else None
        if data.idVar and (not variant or variant.idprod != data.idProd):
            raise DomainError(422, 'variante_invalida', 'La talla y color no pertenecen al producto seleccionado')
        row = db.scalar(select(PoliticaDevolucion).where(PoliticaDevolucion.idprod == data.idProd, PoliticaDevolucion.idvar == data.idVar))
        if row is None:
            row = PoliticaDevolucion(idprod=data.idProd, idvar=data.idVar)
            db.add(row)
        row.dias, row.porcentaje = data.dias, data.porcentaje
        db.flush(); record(db, 'politica_devolucion_guardada', actor, peer, True)
        return policy_view(row)


def sale(db, nro, scope_id):
    row = db.scalar(select(Venta).where(Venta.nroventa == nro).with_for_update())
    if row is None:
        raise DomainError(404, 'venta_no_encontrada', 'Venta no encontrada')
    branch(scope_id, row.nrosuc)
    if row.estado != 'registrada' or not db.scalar(select(Pago.idpago).where(Pago.nroventa == nro, Pago.estado == 'aprobado')):
        raise DomainError(409, 'venta_no_pagada', 'La devolución requiere una venta pagada')
    return row


def view(db, row):
    details = db.scalars(select(DetalleDev).where(DetalleDev.nrodev == row.nrodev)).all()
    items = []
    for d in details:
        line = db.get(DetalleVenta, (d.nroventa, d.iddetalleventa))
        variant = db.get(VarianteProd, line.idvar)
        product = db.get(Producto, variant.idprod)
        items.append(dict(producto=product.descripcion, cantidad=d.cantidad, idDetalleVenta=d.iddetalleventa))
    refund = db.scalar(select(Reembolso).where(Reembolso.nrodev == row.nrodev))
    return dict(nroDev=row.nrodev, nroVenta=row.nroventa, fechaHora=row.fechahora,
                estado='cancelada' if row.estado == 'rechazada' else row.estado,
                motivo=row.motivo, monto=row.monto, items=items,
                reembolso=None if refund is None else dict(idReembolso=refund.idreembolso, metodo=refund.metodo, monto=refund.monto, estado=refund.estado))


def listing(db, scope_id, offset, limit):
    query = select(Devolucion).join(Venta, Venta.nroventa == Devolucion.nroventa)
    if scope_id is not None:
        query = query.where(Venta.nrosuc == scope_id)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    return dict(items=[view(db, d) for d in db.scalars(query.order_by(Devolucion.nrodev.desc()).offset(offset).limit(limit))], total=total, offset=offset, limit=limit)


def create(db, data, actor, scope_id, peer):
    with transaction(db):
        row = sale(db, data.nroVenta, scope_id)
        if row.idusuariocl != data.idCliente:
            raise DomainError(404, 'venta_no_encontrada', 'La venta no corresponde al cliente indicado')
        if len({i.idDetalleVenta for i in data.items}) != len(data.items):
            raise DomainError(422, 'datos_invalidos', 'No repitas líneas de venta')
        amount = Decimal('0')
        for item in data.items:
            line = db.get(DetalleVenta, (row.nroventa, item.idDetalleVenta))
            if line is None:
                raise DomainError(404, 'prenda_no_encontrada', 'La prenda no pertenece a la venta')
            variant = db.get(VarianteProd, line.idvar)
            policies = db.scalars(select(PoliticaDevolucion).where(PoliticaDevolucion.idprod == variant.idprod)).all()
            policy = next((p for p in policies if p.idvar == line.idvar), None) or next((p for p in policies if p.idvar is None), None)
            if policy is None:
                raise DomainError(409, 'politica_no_configurada', 'Configura la política de devolución de esta prenda antes de registrar la solicitud')
            if datetime.now() > row.fechahora + timedelta(days=policy.dias):
                raise DomainError(422, 'plazo_vencido', 'La prenda está fuera del plazo de devolución')
            returned = db.scalar(select(func.coalesce(func.sum(DetalleDev.cantidad), 0)).join(Devolucion, Devolucion.nrodev == DetalleDev.nrodev).where(DetalleDev.nroventa == row.nroventa, DetalleDev.iddetalleventa == item.idDetalleVenta, Devolucion.estado.in_(['pendiente', 'aprobada'])))
            if item.cantidad + returned > line.cantidad:
                raise DomainError(409, 'prenda_devuelta', 'La cantidad ya fue devuelta o tiene una solicitud pendiente')
            if line.descuentounitario is None and row.desc_aplicado > 0:
                raise DomainError(409, 'historico_no_desglosado', 'Esta venta antigua requiere revisión del descuento por prenda antes de reembolsar')
            net = line.preciounitario - (line.descuentounitario or Decimal('0'))
            amount += moneda(net * item.cantidad * policy.porcentaje / 100)
        if is_postgresql(db):
            # La PA oficial registra devolución y detalle en una sola llamada; el
            # monto se calcula arriba en el backend y el motivo se completa aquí
            # (la firma de la PA no lo recibe).
            nro_dev = repo.registrar_con_procedimiento(
                db, nro_venta=row.nroventa, monto=amount,
                id_detalle_ventas=[i.idDetalleVenta for i in data.items],
                cantidades=[i.cantidad for i in data.items])
            request = db.get(Devolucion, nro_dev)
            request.motivo = data.motivo
            db.flush()
        else:
            request = Devolucion(nroventa=row.nroventa, monto=amount, estado='pendiente', motivo=data.motivo)
            db.add(request); db.flush()
            for i, item in enumerate(data.items, 1):
                db.add(DetalleDev(nrodev=request.nrodev, iddetalledev=i, nroventa=row.nroventa, iddetalleventa=item.idDetalleVenta, cantidad=item.cantidad))
            db.flush()
        record(db, 'devolucion_registrada', actor, peer, True)
        return view(db, request)


def decide(db, nro, data, actor, scope_id, peer):
    with transaction(db):
        row = db.scalar(select(Devolucion).where(Devolucion.nrodev == nro).with_for_update())
        if row is None:
            raise DomainError(404, 'devolucion_no_encontrada', 'Devolución no encontrada')
        sold = sale(db, row.nroventa, scope_id)
        if row.estado != 'pendiente':
            return view(db, row)
        if data.accion == 'rechazar':
            row.estado = 'rechazada'
        else:
            if not data.buenEstado:
                raise DomainError(422, 'prenda_danada', 'Solo se aceptan prendas verificadas en buen estado')
            paid = db.scalars(select(Pago).where(Pago.nroventa == row.nroventa, Pago.estado == 'aprobado').with_for_update()).all()
            if len(paid) != 1:
                raise DomainError(409, 'pago_inconsistente', 'Revisa el pago original de esta venta')
            payment = paid[0]
            refunded = db.scalar(select(func.coalesce(func.sum(Reembolso.monto), 0)).where(Reembolso.idpago == payment.idpago, Reembolso.estado != 'rechazado'))
            if refunded + row.monto > payment.monto:
                raise DomainError(409, 'reembolso_excesivo', 'El reembolso supera el importe pagado')
            details = db.scalars(select(DetalleDev).where(DetalleDev.nrodev == nro)).all()
            for detail in sorted(details, key=lambda d: d.iddetalleventa):
                line = db.get(DetalleVenta, (detail.nroventa, detail.iddetalleventa))
                inventories = comercio.locked_inventarios(db, line.idvar, sold.nrosuc)
                if inventories:
                    continue
                # La reposición de stock la realiza el trigger trg_reponer_stock_devolucion
                # en PostgreSQL; aquí solo se asegura que exista la fila de inventario.
                db.add(Inventario(idvar=line.idvar, nrosuc=sold.nrosuc, stock=0, cantdisp=0))
                db.flush()
            if is_postgresql(db):
                # La PA marca 'aprobada'; el trigger repone el stock una sola vez.
                repo.aprobar_con_procedimiento(db, nro)
                db.expire(row)
                row = db.get(Devolucion, nro)
            else:
                # En SQLite (pruebas) no hay triggers: se repone y se registra el
                # movimiento aquí, replicando exactamente lo que haría el trigger.
                for detail in sorted(details, key=lambda d: d.iddetalleventa):
                    line = db.get(DetalleVenta, (detail.nroventa, detail.iddetalleventa))
                    inventory = comercio.locked_inventarios(db, line.idvar, sold.nrosuc)[0]
                    inventory.stock += detail.cantidad; inventory.cantdisp += detail.cantidad
                    db.add(MovimientoInv(nroinv=inventory.nroinv, tipomov='entrada', cantidad=detail.cantidad, fecha=datetime.now().date(), motivo=f'devolucion aprobada nro {nro}'))
                row.estado = 'aprobada'
            db.add(Reembolso(nrodev=nro, idpago=payment.idpago, metodo=payment.metodo, monto=row.monto, estado='pendiente'))
        db.flush(); record(db, 'devolucion_' + data.accion, actor, peer, True)
        return view(db, row)


def refund(db, nro, actor, scope_id, peer, stripe=None):
    with transaction(db):
        row = db.get(Devolucion, nro)
        if row is None:
            raise DomainError(404, 'devolucion_no_encontrada', 'Devolución no encontrada')
        sale(db, row.nroventa, scope_id)
        reimbursement = db.scalar(select(Reembolso).where(Reembolso.nrodev == nro).with_for_update())
        if reimbursement is None:
            raise DomainError(409, 'devolucion_no_aprobada', 'Primero acepta la devolución')
        if reimbursement.estado == 'aprobado':
            return view(db, row)
        payment = db.get(Pago, reimbursement.idpago)
        snapshot = (reimbursement.idreembolso, reimbursement.monto, payment.referencia, payment.metodo)
        if payment.metodo == 'efectivo' or reimbursement.monto == 0:
            reimbursement.estado = 'aprobado'
            record(db, 'reembolso_efectivo_entregado', actor, peer, True)
            return view(db, row)
        if not stripe or not payment.referencia or not payment.referencia.startswith('cs_test_'):
            raise DomainError(503, 'pasarela_no_disponible', 'El reembolso electrónico necesita la pasarela original; no se confirmó entrega de dinero')
    id_refund, amount, reference, method = snapshot
    session = stripe.retrieve(reference)
    result = stripe.refund_partial(session, amount, id_refund)
    with transaction(db):
        reimbursement = db.scalar(select(Reembolso).where(Reembolso.idreembolso == id_refund).with_for_update())
        reimbursement.referencia = result.get('id')
        reimbursement.estado = 'aprobado' if result.get('status') == 'succeeded' else 'rechazado' if result.get('status') in {'failed', 'canceled'} else 'pendiente'
        record(db, 'reembolso_consultado', actor, peer, True)
        return view(db, db.get(Devolucion, nro))
