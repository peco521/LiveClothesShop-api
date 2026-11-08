import hashlib
import json
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import and_, func, select, text

from app.core.database import is_postgresql
from app.core.errors import DomainError
from app.integrations.payments.modos import es_sesion_de_pasarela
from app.modules.cliente_experiencia_compra.shared.models.catalogo import (
    Inventario,
    Producto,
    VarianteProd,
)
from app.modules.cliente_experiencia_compra.shared.models.comercio import (
    DetalleVenta,
    MovimientoInv,
    Pago,
    Venta,
)
from app.modules.cliente_experiencia_compra.shared.repositories import comercio
from app.modules.cliente_experiencia_compra.shared.repositories.tenant import insert_identity
from app.modules.cliente_experiencia_compra.shared.services.precios import moneda
from app.modules.inventario_productos.shared.access import transaction
from app.modules.inventario_productos.shared.operaciones_access import branch
from app.modules.seguridad_accesos.services.bitacora import record

from ..models.devoluciones import (
    Cambio,
    Devolucion,
    DetalleCambio,
    DetalleDev,
    PoliticaDevolucion,
    Reembolso,
)
from ..repositories import devoluciones as repo


def _multitenant(db) -> bool:
    return db.info.get("multitenant") is True


def _tenant(db) -> int | None:
    value = db.info.get("idemp")
    return value if isinstance(value, int) else None


def _request_hash(data) -> str:
    payload = data.model_dump(mode="json", exclude_none=True)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _existing_operation(db, operation_id, request_hash):
    if not _multitenant(db):
        return None
    tenant = _tenant(db)
    if tenant is None:
        raise DomainError(403, "tenant_no_disponible", "La sesión no contiene una empresa")
    db.execute(
        text("SELECT pg_advisory_xact_lock(:tenant, hashtext(:operation)::integer)"),
        {"tenant": tenant, "operation": str(operation_id)},
    )
    existing = db.scalar(
        select(Devolucion).where(Devolucion.idoperacion == operation_id)
    )
    if existing is None:
        return None
    if existing.huellasolicitud != request_hash:
        raise DomainError(
            409,
            "operacion_reutilizada",
            "El identificador de operación ya fue usado con otros datos",
        )
    return existing


def policy_view(row, db=None):
    result = dict(id=row.id, idProd=row.idprod, idVar=row.idvar, dias=row.dias, porcentaje=row.porcentaje)
    if db is not None:
        from app.modules.cliente_experiencia_compra.shared.repositories import catalogo

        product = db.get(Producto, row.idprod)
        variant = db.get(VarianteProd, row.idvar) if row.idvar else None
        result["producto"] = product.descripcion if product else "Prenda no disponible"
        if variant:
            talla = catalogo.tallas_map(db, [variant.idtalla]).get(variant.idtalla)
            colors = catalogo.variant_colors(db, [variant.idvariante]).get(variant.idvariante, [])
            result["opcion"] = "Talla " + (talla.descripcion if talla else "") + " · " + ", ".join(
                color.descripcion for color in colors
            )
        else:
            result["opcion"] = "Todo el producto"
    return result


def policy_save(db, data, actor, peer):
    with transaction(db):
        product = db.scalar(
            select(Producto).where(Producto.idprod == data.idProd).with_for_update()
        )
        if not product:
            raise DomainError(404, "producto_no_encontrado", "Producto no encontrado")
        variant = db.get(VarianteProd, data.idVar) if data.idVar else None
        if data.idVar and (not variant or variant.idprod != data.idProd):
            raise DomainError(422, "variante_invalida", "La talla y color no pertenecen al producto seleccionado")
        query = select(PoliticaDevolucion).where(
            PoliticaDevolucion.idprod == data.idProd,
            PoliticaDevolucion.idvar == data.idVar,
        )
        row = db.scalar(query.with_for_update())
        if row is None:
            row = PoliticaDevolucion(idprod=data.idProd, idvar=data.idVar)
            db.add(row)
        row.dias, row.porcentaje = data.dias, data.porcentaje
        db.flush()
        record(db, "politica_devolucion_guardada", actor, peer, True)
        return policy_view(row)


def sale(db, nro, scope_id):
    row = db.scalar(select(Venta).where(Venta.nroventa == nro).with_for_update())
    if row is None:
        raise DomainError(404, "venta_no_encontrada", "Venta no encontrada")
    branch(scope_id, row.nrosuc)
    if row.estado != "registrada" or not db.scalar(
        select(Pago.idpago).where(Pago.nroventa == nro, Pago.estado == "aprobado")
    ):
        raise DomainError(409, "venta_no_pagada", "La devolución requiere una venta pagada")
    return row


def _policy_for_line(db, line, variant, sold):
    if _multitenant(db):
        days = line.diasdevolucionaplicado
        percentage = line.porcentajedevolucionaplicado
        if days is None or percentage is None:
            raise DomainError(
                409,
                "politica_historica_ausente",
                "La venta no conserva una política completa; requiere revisión manual",
            )
    else:
        policies = db.scalars(
            select(PoliticaDevolucion).where(PoliticaDevolucion.idprod == variant.idprod)
        ).all()
        policy = next((item for item in policies if item.idvar == line.idvar), None)
        policy = policy or next((item for item in policies if item.idvar is None), None)
        if policy is None:
            raise DomainError(
                409,
                "politica_no_configurada",
                "Configura la política de devolución de esta prenda antes de registrar la solicitud",
            )
        days, percentage = policy.dias, policy.porcentaje
    if datetime.now() > sold.fechahora + timedelta(days=days):
        raise DomainError(422, "plazo_vencido", "La prenda está fuera del plazo de devolución")
    if line.descuentounitario is None and sold.desc_aplicado > 0:
        raise DomainError(
            409,
            "historico_no_desglosado",
            "Esta venta antigua requiere revisión del descuento por prenda antes de reembolsar",
        )
    return percentage


def _credit_for_line(line, quantity, percentage):
    net = line.preciounitario - (line.descuentounitario or Decimal("0"))
    if net < 0:
        raise DomainError(409, "precio_historico_invalido", "El precio neto de la venta no es válido")
    return moneda(net * quantity * percentage / 100)


def _remaining_quantity(db, sold, line):
    returned = db.scalar(
        select(func.coalesce(func.sum(DetalleDev.cantidad), 0))
        .join(Devolucion, Devolucion.nrodev == DetalleDev.nrodev)
        .where(
            DetalleDev.nroventa == sold.nroventa,
            DetalleDev.iddetalleventa == line.iddetalleventa,
            Devolucion.estado.in_(["pendiente", "aprobada"]),
        )
    ) or 0
    changed = 0
    if _multitenant(db):
        changed = db.scalar(
            select(func.coalesce(func.sum(DetalleCambio.cantidad), 0))
            .join(Cambio, Cambio.nrocambio == DetalleCambio.nrocambio)
            .where(
                DetalleCambio.nroventa == sold.nroventa,
                DetalleCambio.iddetalleventa == line.iddetalleventa,
                Cambio.estado.in_(["pendiente", "aprobado"]),
            )
        ) or 0
    return line.cantidad - returned - changed


def _validate_request_lines(db, sold, items, *, exchange=False):
    if len({item.idDetalleVenta for item in items}) != len(items):
        raise DomainError(422, "datos_invalidos", "No repitas líneas de venta")
    amount = Decimal("0")
    lines = []
    for item in items:
        line = db.get(DetalleVenta, (sold.nroventa, item.idDetalleVenta))
        if line is None:
            raise DomainError(404, "prenda_no_encontrada", "La prenda no pertenece a la venta")
        if item.cantidad > _remaining_quantity(db, sold, line):
            raise DomainError(
                409,
                "prenda_devuelta",
                "La cantidad ya fue devuelta, cambiada o tiene una solicitud pendiente",
            )
        variant = db.get(VarianteProd, line.idvar)
        percentage = _policy_for_line(db, line, variant, sold)
        credit = _credit_for_line(line, item.cantidad, percentage)
        amount += credit
        lines.append((item, line, variant, percentage, credit))
    return moneda(amount), lines


def _register_return(db, data, sold, amount, actor, peer, request_hash):
    if is_postgresql(db):
        nro_dev = repo.registrar_con_procedimiento(
            db,
            idemp=_tenant(db),
            nro_venta=sold.nroventa,
            monto=amount,
            id_detalle_ventas=[item.idDetalleVenta for item in data.items],
            cantidades=[item.cantidad for item in data.items],
        )
        request = db.get(Devolucion, nro_dev)
    else:
        request = Devolucion(
            nroventa=sold.nroventa,
            monto=amount,
            estado="pendiente",
            motivo=data.motivo,
        )
        db.add(request)
        db.flush()
        for index, item in enumerate(data.items, 1):
            db.add(
                DetalleDev(
                    nrodev=request.nrodev,
                    iddetalledev=index,
                    nroventa=sold.nroventa,
                    iddetalleventa=item.idDetalleVenta,
                    cantidad=item.cantidad,
                )
            )
        db.flush()
    request.motivo = data.motivo
    if _multitenant(db):
        request.idoperacion = data.idOperacion
        request.huellasolicitud = request_hash
    db.flush()
    record(db, "devolucion_registrada", actor, peer, True, resource_id=str(request.nrodev))
    return request


def create(db, data, actor, scope_id, peer):
    with transaction(db):
        request_hash = _request_hash(data)
        duplicate = _existing_operation(db, data.idOperacion, request_hash)
        if duplicate is not None:
            return view(db, duplicate)
        sold = sale(db, data.nroVenta, scope_id)
        if sold.idusuariocl != data.idCliente:
            raise DomainError(404, "venta_no_encontrada", "La venta no corresponde al cliente indicado")
        amount, _ = _validate_request_lines(db, sold, data.items)
        request = _register_return(db, data, sold, amount, actor, peer, request_hash)
        return view(db, request)


def _exchange_view(db, request, returned_items):
    if not _multitenant(db):
        return None
    change = db.scalar(
        select(Cambio).where(Cambio.nrodev == request.nrodev)
    )
    if change is None:
        return None
    new_sale = db.get(Venta, change.nroventanueva) if change.nroventanueva else None
    payments = (
        db.scalars(select(Pago).where(Pago.nroventa == new_sale.nroventa)).all()
        if new_sale
        else []
    )
    change_items = db.scalars(
        select(DetalleCambio).where(DetalleCambio.nrocambio == change.nrocambio)
    ).all()
    returned_conditions = {
        item["idDetalleVenta"]: item["condicion"] for item in returned_items
    }
    exchange_items = []
    for item in change_items:
        variant = db.get(VarianteProd, item.idvarnueva)
        product = db.get(Producto, variant.idprod) if variant else None
        exchange_items.append(
            dict(
                idDetalleVenta=item.iddetalleventa,
                cantidad=item.cantidad,
                condicion=returned_conditions.get(item.iddetalleventa),
                varianteNueva=item.idvarnueva,
                productoNuevo=product.descripcion if product else "Prenda no disponible",
                precioNuevo=item.precionuevo,
            )
        )
    return dict(
        nroCambio=change.nrocambio,
        estado=change.estado,
        nroVentaNueva=change.nroventanueva,
        diferencia=change.difmonto,
        estadoPago=next((payment.estado for payment in payments if payment.estado == "aprobado"), None),
        items=exchange_items,
    )


def view(db, row):
    details = db.scalars(select(DetalleDev).where(DetalleDev.nrodev == row.nrodev)).all()
    items = []
    for detail in details:
        line = db.get(DetalleVenta, (detail.nroventa, detail.iddetalleventa))
        variant = db.get(VarianteProd, line.idvar)
        product = db.get(Producto, variant.idprod)
        items.append(
            dict(
                producto=product.descripcion if product else "Prenda no disponible",
                cantidad=detail.cantidad,
                idDetalleVenta=detail.iddetalleventa,
                condicion=detail.condicion if _multitenant(db) else None,
            )
        )
    refund = db.scalar(select(Reembolso).where(Reembolso.nrodev == row.nrodev))
    return dict(
        nroDev=row.nrodev,
        nroVenta=row.nroventa,
        fechaHora=row.fechahora,
        estado="cancelada" if row.estado == "rechazada" else row.estado,
        motivo=row.motivo,
        monto=row.monto,
        items=items,
        cambio=_exchange_view(db, row, items),
        reembolso=None
        if refund is None
        else dict(
            idReembolso=refund.idreembolso,
            metodo=refund.metodo,
            monto=refund.monto,
            estado=refund.estado,
        ),
    )


def listing(db, scope_id, offset, limit):
    join_condition = Venta.nroventa == Devolucion.nroventa
    if _multitenant(db):
        join_condition = and_(join_condition, Venta.idemp == Devolucion.idemp)
    query = select(Devolucion).join(Venta, join_condition)
    if scope_id is not None:
        query = query.where(Venta.nrosuc == scope_id)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = db.scalars(
        query.order_by(Devolucion.nrodev.desc()).offset(offset).limit(limit)
    ).all()
    return dict(items=[view(db, row) for row in rows], total=total, offset=offset, limit=limit)


def exchange_variants(db, nro, scope_id):
    sold = sale(db, nro, scope_id)
    rows = db.execute(
        select(
            VarianteProd.idvariante,
            VarianteProd.sku,
            Producto.descripcion,
            VarianteProd.precio,
            func.coalesce(func.sum(Inventario.cantdisp), 0),
        )
        .join(Producto, and_(Producto.idprod == VarianteProd.idprod, Producto.estado == "activo"))
        .join(
            Inventario,
            and_(
                Inventario.idvar == VarianteProd.idvariante,
                Inventario.nrosuc == sold.nrosuc,
            ),
        )
        .where(VarianteProd.estado == "activo")
        .group_by(
            VarianteProd.idvariante,
            VarianteProd.sku,
            Producto.descripcion,
            VarianteProd.precio,
        )
        .order_by(Producto.descripcion, VarianteProd.sku)
    ).all()
    return [
        dict(idVar=row[0], sku=row[1], producto=row[2], precio=row[3], disponible=row[4])
        for row in rows
        if row[4] > 0
    ]


def create_exchange(db, data, actor, scope_id, peer):
    if not _multitenant(db) or not is_postgresql(db):
        raise DomainError(503, "cambio_no_disponible", "Los cambios requieren el esquema multitenant PostgreSQL")
    with transaction(db):
        request_hash = _request_hash(data)
        duplicate = _existing_operation(db, data.idOperacion, request_hash)
        if duplicate is not None:
            return view(db, duplicate)
        sold = sale(db, data.nroVenta, scope_id)
        if sold.idusuariocl != data.idCliente:
            raise DomainError(404, "venta_no_encontrada", "La venta no corresponde al cliente indicado")
        old_credit, lines = _validate_request_lines(db, sold, data.items, exchange=True)
        new_total = Decimal("0")
        new_lines = []
        for item, old_line, _, percentage, credit in lines:
            if item.cantidad <= 0:
                raise DomainError(422, "datos_invalidos", "La cantidad debe ser positiva")
            variant = db.scalar(
                select(VarianteProd)
                .where(
                    VarianteProd.idvariante == item.idVarNueva,
                    VarianteProd.estado == "activo",
                )
                .with_for_update()
            )
            if variant is None or variant.idvariante == old_line.idvar:
                raise DomainError(404, "variante_no_encontrada", "La variante nueva no está disponible")
            product = db.get(Producto, variant.idprod)
            if product is None or product.estado != "activo":
                raise DomainError(404, "variante_no_encontrada", "La variante nueva no está disponible")
            inventory = comercio.locked_inventarios(db, variant.idvariante, sold.nrosuc)
            if sum(row.cantdisp for row in inventory) < item.cantidad:
                raise DomainError(409, "disponibilidad_insuficiente", "No hay disponibilidad en la sucursal de la venta")
            new_total += moneda(variant.precio * item.cantidad)
            new_lines.append((item, variant, percentage, credit))
        new_total = moneda(new_total)
        difference = moneda(new_total - old_credit)
        request = _register_return(db, data, sold, old_credit, actor, peer, request_hash)
        employee_id = None
        if scope_id is not None:
            employee_id = actor
        change = Cambio(
            estado="pendiente",
            motivo=data.motivo,
            difmonto=difference,
            nroventa=sold.nroventa,
            nrosuc=sold.nrosuc,
            idusuarioemp=employee_id,
            nrodev=request.nrodev,
        )
        change = insert_identity(db, change)

        applied_credit = min(old_credit, new_total)
        new_sale = Venta(
            nit=None,
            total=new_total - applied_credit,
            desc_aplicado=applied_credit,
            estado="registrada",
            idusuariocl=sold.idusuariocl,
            idusuarioemp=employee_id,
            nrosuc=sold.nrosuc,
            idcarrito=None,
            nroreserva=None,
        )
        new_sale = insert_identity(db, new_sale)
        change.nroventanueva = new_sale.nroventa
        remaining_credit = applied_credit
        detail_index = 0
        for exchange_index, (item, variant, _, _) in enumerate(new_lines, 1):
            line_credit = min(remaining_credit, variant.precio * item.cantidad)
            credit_cents = int(line_credit * 100)
            base_cents, remainder = divmod(credit_cents, item.cantidad)
            base_discount = Decimal(base_cents) / 100
            base_quantity = item.cantidad - remainder
            if base_quantity:
                detail_index += 1
                db.add(
                    DetalleVenta(
                        nroventa=new_sale.nroventa,
                        iddetalleventa=detail_index,
                        preciounitario=variant.precio,
                        descuentounitario=base_discount,
                        cantidad=base_quantity,
                        idvar=variant.idvariante,
                    )
                )
            if remainder:
                detail_index += 1
                db.add(
                    DetalleVenta(
                        nroventa=new_sale.nroventa,
                        iddetalleventa=detail_index,
                        preciounitario=variant.precio,
                        descuentounitario=base_discount + Decimal("0.01"),
                        cantidad=remainder,
                        idvar=variant.idvariante,
                    )
                )
            remaining_credit -= line_credit
            db.add(
                DetalleCambio(
                    nrocambio=change.nrocambio,
                    iddetallecambio=exchange_index,
                    cantidad=item.cantidad,
                    nroventa=sold.nroventa,
                    iddetalleventa=item.idDetalleVenta,
                    idvarnueva=variant.idvariante,
                    precionuevo=variant.precio,
                )
            )
        db.flush()
        record(db, "cambio_registrado", actor, peer, True, resource_id=str(change.nrocambio))
        return view(db, request)


def _apply_inspections(db, row, inspections, *, required):
    details = db.scalars(
        select(DetalleDev).where(DetalleDev.nrodev == row.nrodev).order_by(DetalleDev.iddetalledev)
    ).all()
    by_line = {item.idDetalleVenta: item.condicion for item in inspections}
    if inspections and len(by_line) != len(inspections):
        raise DomainError(422, "datos_invalidos", "No repitas líneas de inspección")
    valid_ids = {detail.iddetalleventa for detail in details}
    if set(by_line) - valid_ids:
        raise DomainError(422, "datos_invalidos", "La inspección incluye una prenda ajena a la solicitud")
    for detail in details:
        condition = by_line.get(detail.iddetalleventa)
        if condition is not None:
            detail.condicion = condition
        if required and detail.condicion not in {"apta", "danada"}:
            raise DomainError(422, "inspeccion_incompleta", "Inspecciona cada línea antes de aprobar")
    return details


def _approved_original_payment(db, nro_venta):
    payments = db.scalars(
        select(Pago)
        .where(Pago.nroventa == nro_venta, Pago.estado == "aprobado")
        .with_for_update()
    ).all()
    if len(payments) != 1:
        raise DomainError(409, "pago_inconsistente", "Revisa el pago original de esta venta")
    return payments[0]


def _approve_return(db, row):
    if is_postgresql(db):
        repo.aprobar_con_procedimiento(db, idemp=_tenant(db), nro_dev=row.nrodev)
        db.expire(row)
        return db.get(Devolucion, row.nrodev)
    row.estado = "aprobada"
    for detail in db.scalars(
        select(DetalleDev).where(DetalleDev.nrodev == row.nrodev).order_by(DetalleDev.iddetalleventa)
    ):
        line = db.get(DetalleVenta, (detail.nroventa, detail.iddetalleventa))
        inventory_rows = comercio.locked_inventarios(db, line.idvar, db.get(Venta, row.nroventa).nrosuc)
        if not inventory_rows:
            inventory = Inventario(
                idvar=line.idvar,
                nrosuc=db.get(Venta, row.nroventa).nrosuc,
                stock=0,
                cantdisp=0,
            )
            db.add(inventory)
            db.flush()
            inventory_rows = [inventory]
        inventory = inventory_rows[0]
        inventory.stock += detail.cantidad
        inventory.cantdisp += detail.cantidad
        db.add(
            MovimientoInv(
                nroinv=inventory.nroinv,
                tipomov="entrada",
                cantidad=detail.cantidad,
                fecha=datetime.now().date(),
                motivo=f"devolucion aprobada nro {row.nrodev}",
            )
        )
    return row


def _approve_change(db, change, actor, peer):
    if change is None or change.estado == "aprobado":
        return
    if change.estado != "pendiente":
        raise DomainError(409, "cambio_no_pendiente", "El cambio ya fue procesado")
    repo.aprobar_cambio_con_procedimiento(
        db,
        idemp=_tenant(db),
        nro_cambio=change.nrocambio,
    )
    db.refresh(change)
    record(db, "cambio_aprobado", actor, peer, True, resource_id=str(change.nrocambio))


def decide(db, nro, data, actor, scope_id, peer):
    with transaction(db):
        row = db.scalar(
            select(Devolucion).where(Devolucion.nrodev == nro).with_for_update()
        )
        if row is None:
            raise DomainError(404, "devolucion_no_encontrada", "Devolución no encontrada")
        sold = sale(db, row.nroventa, scope_id)
        if row.estado != "pendiente":
            return view(db, row)
        change = None
        if _multitenant(db):
            change = db.scalar(
                select(Cambio).where(Cambio.nrodev == row.nrodev).with_for_update()
            )
        if data.accion == "rechazar":
            if change is not None:
                new_sale = db.scalar(
                    select(Venta)
                    .where(Venta.nroventa == change.nroventanueva)
                    .with_for_update()
                )
                payments = db.scalars(
                    select(Pago).where(Pago.nroventa == new_sale.nroventa).with_for_update()
                ).all()
                if any(payment.estado in {"pendiente", "aprobado"} for payment in payments):
                    raise DomainError(409, "cambio_en_pago", "No se puede rechazar un cambio con pago pendiente o aprobado")
                new_sale.estado = "anulada"
                change.estado = "rechazado"
                record(db, "cambio_rechazado", actor, peer, True, resource_id=str(change.nrocambio))
            row.estado = "rechazada"
            db.flush()
            record(db, "devolucion_rechazada", actor, peer, True, resource_id=str(row.nrodev))
            return view(db, row)

        details = _apply_inspections(
            db,
            row,
            data.inspecciones,
            required=data.accion in {"inspeccionar", "aprobar"},
        )
        db.flush()
        if data.accion == "inspeccionar":
            record(db, "devolucion_inspeccionada", actor, peer, True, resource_id=str(row.nrodev))
            return view(db, row)

        payment = _approved_original_payment(db, row.nroventa)
        prior_refunds = db.scalar(
            select(func.coalesce(func.sum(Reembolso.monto), 0)).where(
                Reembolso.idpago == payment.idpago,
                Reembolso.estado != "rechazado",
            )
        ) or Decimal("0")
        if change is None:
            refund_amount = row.monto
        elif change.difmonto < 0:
            refund_amount = abs(change.difmonto)
        else:
            refund_amount = Decimal("0")
        if prior_refunds + refund_amount > payment.monto:
            raise DomainError(409, "reembolso_excesivo", "El reembolso supera el importe pagado")

        if change is not None and change.difmonto > 0:
            new_payments = db.scalars(
                select(Pago)
                .where(Pago.nroventa == change.nroventanueva)
                .with_for_update()
            ).all()
            if not any(payment_row.estado == "aprobado" for payment_row in new_payments):
                raise DomainError(409, "diferencia_pendiente", "La diferencia del cambio aún no está pagada")

        row = _approve_return(db, row)
        if refund_amount > 0:
            db.add(
                Reembolso(
                    nrodev=row.nrodev,
                    idpago=payment.idpago,
                    metodo=payment.metodo,
                    monto=refund_amount,
                    estado="pendiente",
                )
            )
        db.flush()
        if change is not None and change.difmonto >= 0:
            _approve_change(db, change, actor, peer)
        record(db, "devolucion_aprobada", actor, peer, True, resource_id=str(row.nrodev))
        return view(db, row)


def _complete_exchange_after_refund(db, row, actor, peer):
    if not _multitenant(db):
        return
    change = db.scalar(
        select(Cambio).where(Cambio.nrodev == row.nrodev).with_for_update()
    )
    if change is not None and change.difmonto < 0:
        _approve_change(db, change, actor, peer)


def refund(db, nro, actor, scope_id, peer, stripe=None):
    with transaction(db):
        row = db.scalar(
            select(Devolucion).where(Devolucion.nrodev == nro).with_for_update()
        )
        if row is None:
            raise DomainError(404, "devolucion_no_encontrada", "Devolución no encontrada")
        sale(db, row.nroventa, scope_id)
        reimbursement = db.scalar(
            select(Reembolso).where(Reembolso.nrodev == nro).with_for_update()
        )
        if reimbursement is None:
            raise DomainError(409, "devolucion_no_aprobada", "Primero acepta la devolución")
        if reimbursement.estado == "aprobado":
            return view(db, row)
        payment = db.get(Pago, reimbursement.idpago)
        if payment is None:
            raise DomainError(409, "pago_inconsistente", "No se encuentra el pago vinculado al reembolso")
        snapshot = (
            reimbursement.idreembolso,
            reimbursement.monto,
            payment.referencia,
            payment.metodo,
        )
        if payment.metodo == "efectivo" or reimbursement.monto == 0:
            reimbursement.estado = "aprobado"
            _complete_exchange_after_refund(db, row, actor, peer)
            record(
                db,
                "reembolso_efectivo_entregado",
                actor,
                peer,
                True,
                resource_id=str(reimbursement.idreembolso),
            )
            return view(db, row)
        if not stripe or not es_sesion_de_pasarela(payment.referencia):
            raise DomainError(
                503,
                "pasarela_no_disponible",
                "El reembolso electrónico necesita la pasarela original; no se confirmó entrega de dinero",
            )

    refund_id, amount, reference, _ = snapshot
    session = stripe.retrieve(reference)
    result = stripe.refund_partial(session, amount, refund_id)
    with transaction(db):
        reimbursement = db.scalar(
            select(Reembolso)
            .where(Reembolso.idreembolso == refund_id)
            .with_for_update()
        )
        status = result.get("status")
        reimbursement.referencia = result.get("id")
        reimbursement.estado = (
            "aprobado"
            if status == "succeeded"
            else "rechazado"
            if status in {"failed", "canceled"}
            else "pendiente"
        )
        row = db.get(Devolucion, nro)
        if reimbursement.estado == "aprobado":
            _complete_exchange_after_refund(db, row, actor, peer)
        record(
            db,
            "reembolso_consultado",
            actor,
            peer,
            True,
            resource_id=str(reimbursement.idreembolso),
        )
        return view(db, row)
