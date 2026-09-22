"""Checkout orchestration with durable session/refund markers in pago.referencia.

No remote Stripe request runs under a DB transaction. Webhooks and explicit
reconciliation are idempotent. No browser-supplied success authorizes stock.
"""
from app.core.errors import DomainError
from app.integrations.payments.modos import es_sesion_de_pasarela, livemode_esperado, modo_de_adaptador
from app.modules.cliente_experiencia_compra.cu12_carrito.repositories import carrito as carts
from app.modules.cliente_experiencia_compra.cu14_pago_electronico.repositories import pago as repo
from app.modules.cliente_experiencia_compra.cu14_pago_electronico.services import pago as core
from app.modules.cliente_experiencia_compra.shared.models.comercio import Pago
from app.modules.seguridad_accesos.services.bitacora import record


def _livemode_esperado(stripe) -> bool:
    """Modo Stripe declarado por la pasarela inyectada: prueba o vivo."""
    return livemode_esperado(modo_de_adaptador(stripe))


def _locked(db, id_pago, user_id):
    carts.locked_cliente(db, user_id)
    pago = repo.locked_pago(db, id_pago)
    venta = repo.locked_venta(db, pago.nroventa) if pago else None
    if not venta or venta.idusuariocl != user_id:
        raise DomainError(404, "pago_no_encontrado", "Pago no encontrado")
    return pago, venta


def _reject(db, pago, venta, user_id, peer):
    """Descarta un pago electrónico que no se cobró.

    La compra digital (CU13/CU14) se anula porque el cobro falló; la venta
    **presencial** de CU24 (`claveOperacion`, sin carrito) sigue `registrada` para
    que la caja pueda cobrarla en efectivo o reintentar la tarjeta sin volver a
    preparar las prendas. Anular la venta es una acción explícita (`cancel_sale`).
    """
    pago.estado = "rechazado"
    if venta.claveoperacion is None:
        # Compra digital (CU13/CU14): el cobro fallo y la compra se anula.
        venta.estado = "anulada"
        record(db, "venta_anulada", user_id, peer, True)
    record(db, "pago_rechazado", user_id, peer, True)
    return core._vista(db, pago)


def _retorno(venta, id_pago, stripe):
    """URLs de retorno de la Checkout Session según el origen de la venta.

    CU13/CU14 (compra digital del cliente) vuelve a la tienda; la venta
    presencial de CU24 (`claveOperacion`, sin carrito) vuelve al comprobante
    administrativo de caja para no enviar al cajero a una pantalla de cliente.
    """
    base = stripe.frontend_url.rstrip("/")
    if venta.claveoperacion is not None:
        return (f"{base}/admin/caja?venta={venta.nroventa}&pago={id_pago}",
                f"{base}/admin/caja?venta={venta.nroventa}&pago={id_pago}&cancelar=1")
    return (f"{base}/tienda/pago/{id_pago}", f"{base}/tienda/pago/{id_pago}?cancelar=1")


def start(db, data, user_id, peer, stripe):
    if data.metodo != "tarjeta" or data.escenario is not None:
        raise DomainError(422, "datos_invalidos", "Stripe Checkout admite tarjeta sin escenarios simulados")
    with core.transaction(db):
        carts.locked_cliente(db, user_id)
        venta = repo.locked_venta(db, data.nroVenta)
        if not venta or venta.idusuariocl != user_id:
            raise DomainError(404, "venta_no_encontrada", "Venta no encontrada")
        if venta.estado != "registrada":
            raise DomainError(409, "venta_no_pagable", "La venta ya no puede pagarse")
        previos = repo.pagos_de_venta(db, venta.nroventa)
        pago = next((p for p in previos if p.estado in {"pendiente", "aprobado"}), None)
        created = pago is None
        if pago is None:
            pago = repo.add_pago(db, Pago(metodo="tarjeta", monto=venta.total, estado="pendiente",
                                         referencia="stripe:creating", nroventa=venta.nroventa))
            record(db, "pago_iniciado", user_id, peer, True)
        if pago.estado == "aprobado":
            return core._vista(db, pago), False
        success_url, cancel_url = _retorno(venta, pago.idpago, stripe)
        snapshot = (pago.idpago, venta.nroventa, pago.monto, pago.referencia, success_url, cancel_url)
    id_pago, nro, monto, reference, success_url, cancel_url = snapshot
    if reference == "stripe:creating":
        session = stripe.create(id_pago, nro, monto, success_url=success_url, cancel_url=cancel_url)
        with core.transaction(db):
            pago, venta = _locked(db, id_pago, user_id)
            if pago.estado != "pendiente":
                return core._vista(db, pago), False
            pago.referencia = session["id"]
    elif reference and es_sesion_de_pasarela(reference):
        session = stripe.retrieve(reference)
    elif reference and reference.startswith("refund:"):
        return reconcile(db, id_pago, user_id, peer, stripe), False
    else:
        raise DomainError(409, "pasarela_incompatible", "La compra tiene un pago de otra pasarela")
    result = reconcile(db, id_pago, user_id, peer, stripe)
    if result.estado == "pendiente" and session.get("status") == "open":
        result.checkoutUrl = session.get("url")
    return result, created


def reconcile(db, id_pago, user_id, peer, stripe):
    with core.transaction(db):
        pago, venta = _locked(db, id_pago, user_id)
        if pago.estado != "pendiente":
            return core._vista(db, pago)
        reference, amount, nro = pago.referencia, pago.monto, venta.nroventa
    if reference == "stripe:creating":
        success_url, cancel_url = _retorno(venta, id_pago, stripe)
        session = stripe.create(id_pago, nro, amount, success_url=success_url, cancel_url=cancel_url)
        with core.transaction(db):
            pago, venta = _locked(db, id_pago, user_id)
            if pago.estado != "pendiente":
                return core._vista(db, pago)
            pago.referencia = session["id"]
        reference = session["id"]
    refunding = bool(reference and reference.startswith("refund:"))
    session_id = reference[7:] if refunding else reference
    session = stripe.retrieve(session_id)
    if (session.get("livemode") is not _livemode_esperado(stripe) or session.get("currency") != stripe.currency
            or session.get("amount_total") != int(amount * 100)
            or session.get("metadata", {}).get("idPago") != str(id_pago)
            or session.get("metadata", {}).get("nroVenta") != str(nro)):
        raise DomainError(409, "pago_inconsistente", "No se pudo verificar el pago")
    paid = session.get("payment_status") == "paid"
    already_refunded = paid and stripe.refunded(session)
    with core.transaction(db):
        pago, venta = _locked(db, id_pago, user_id)
        if pago.estado != "pendiente":
            return core._vista(db, pago)
        # Another reconciliation may already have requested compensation.
        refunding = refunding or pago.referencia.startswith("refund:")
        if already_refunded or session.get("status") == "expired":
            return _reject(db, pago, venta, user_id, peer)
        if not paid:
            return core._vista(db, pago)
        if not refunding:
            try:
                with db.begin_nested():
                    core._descontar(db, venta, user_id, peer)
            except DomainError as error:
                if error.code not in {"disponibilidad_insuficiente", "carrito_modificado", "carrito_no_disponible"}:
                    raise
                pago.referencia = "refund:" + session_id
                refunding = True
            if not refunding:
                pago.estado = "aprobado"
                record(db, "pago_aprobado", user_id, peer, True)
                return core._vista(db, pago)
    # Refund marker is committed BEFORE remote compensation. Retrying never
    # delivers an order that may already have been refunded.
    refund = stripe.refund(session, id_pago)
    if refund.get("status") != "succeeded":
        raise DomainError(503, "reembolso_pendiente", "El reembolso está pendiente de confirmación")
    with core.transaction(db):
        pago, venta = _locked(db, id_pago, user_id)
        if pago.estado == "pendiente":
            return _reject(db, pago, venta, user_id, peer)
        return core._vista(db, pago)


def void_pending(db, nro, user_id, peer, stripe=None):
    """Libera una venta de caja de un pago electronico pendiente que no se cobro.

    La caja necesita poder cambiar de medio de pago: se reconcilia primero (un cobro
    real se aprueba y NO se anula), luego se consulta la sesion y solo se descarta el
    pago cuando la pasarela confirma que no hubo cobro (sesion abierta --se expira-- o
    vencida). Una sesion en proceso se respeta y se informa.
    """
    with core.transaction(db):
        venta = repo.locked_venta(db, nro)
        if not venta or venta.idusuariocl != user_id:
            raise DomainError(404, "venta_no_encontrada", "Venta no encontrada")
        pagos = repo.pagos_de_venta(db, nro)
        aprobado = next((p for p in pagos if p.estado == "aprobado"), None)
        if aprobado is not None:
            return core._vista(db, aprobado)
        pendiente = next((p for p in pagos if p.estado == "pendiente"), None)
        snapshot = (pendiente.idpago, pendiente.referencia) if pendiente else None
    if snapshot is None:
        return None
    id_pago, reference = snapshot
    if stripe is not None and reference and (es_sesion_de_pasarela(reference)
                                             or reference.startswith("refund:")):
        # Reconcilia primero: si el cliente pago, el pago queda aprobado y no se anula.
        vista = reconcile(db, id_pago, user_id, peer, stripe)
        if vista.estado != "pendiente":
            return None
        session_id = reference[7:] if reference.startswith("refund:") else reference
        session = stripe.retrieve(session_id)
        if session.get("payment_status") == "paid":
            raise DomainError(409, "pago_en_curso", "El pago ya fue confirmado; consulta su estado")
        if session.get("status") == "open":
            stripe.expire(session_id)
        elif session.get("status") != "expired":
            raise DomainError(409, "pago_en_curso",
                              "La pasarela aun procesa el pago; espera o anula el pago")
    with core.transaction(db):
        pago, venta = _locked(db, id_pago, user_id)
        if pago.estado == "pendiente":
            return _reject(db, pago, venta, user_id, peer)
    return None

def cancel_sale(db, nro, user_id, peer, stripe=None):
    with core.transaction(db):
        carts.locked_cliente(db, user_id)
        venta = repo.locked_venta(db, nro)
        if not venta or venta.idusuariocl != user_id:
            raise DomainError(404, "venta_no_encontrada", "Venta no encontrada")
        pagos = repo.pagos_de_venta(db, nro)
        if any(p.estado == "aprobado" for p in pagos):
            raise DomainError(409, "venta_no_cancelable", "La compra ya está pagada")
        pending = next((p for p in pagos if p.estado == "pendiente"), None)
        snapshot = (pending.idpago, pending.monto, pending.referencia) if pending else None
    if stripe and snapshot:
        id_pago, amount, reference = snapshot
        if reference == "stripe:creating":
            success_url, cancel_url = _retorno(venta, id_pago, stripe)
            session = stripe.create(id_pago, nro, amount, success_url=success_url, cancel_url=cancel_url)
            with core.transaction(db):
                pago, venta = _locked(db, id_pago, user_id)
                if pago.estado == "pendiente":
                    pago.referencia = session["id"]
        elif reference and (es_sesion_de_pasarela(reference) or reference.startswith("refund:")):
            session = stripe.retrieve(reference[7:] if reference.startswith("refund:") else reference)
        else:
            raise DomainError(409, "pasarela_incompatible", "El pago pertenece a otra pasarela; no cambie de proveedor")
        if session["status"] == "open":
            # If payment wins the race, expiry fails; keep pending and reconcile.
            stripe.expire(session["id"])
        elif session.get("payment_status") == "paid":
            reconcile(db, id_pago, user_id, peer, stripe)
            raise DomainError(409, "venta_no_cancelable", "El pago ya fue confirmado; consulta su estado")
        elif session["status"] != "expired":
            raise DomainError(409, "pago_pendiente", "El pago aún se está procesando")
    elif snapshot and snapshot[2] and (es_sesion_de_pasarela(snapshot[2])
                                      or snapshot[2].startswith("stripe:") or snapshot[2].startswith("refund:")):
        raise DomainError(409, "pasarela_incompatible", "Configure Stripe para cancelar este pago con seguridad")
    with core.transaction(db):
        carts.locked_cliente(db, user_id)
        venta = repo.locked_venta(db, nro)
        pagos = repo.pagos_de_venta(db, nro)
        if any(p.estado == "aprobado" for p in pagos):
            raise DomainError(409, "venta_no_cancelable", "La compra ya está pagada")
        if venta.estado != "anulada":
            for pago in pagos:
                if pago.estado == "pendiente":
                    _reject(db, pago, venta, user_id, peer)
            venta.estado = "anulada"
            if not pagos:
                record(db, "venta_anulada", user_id, peer, True)
        from app.modules.cliente_experiencia_compra.cu13_compra_digital.services.compra import _vista
        return _vista(db, venta)
