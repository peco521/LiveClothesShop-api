from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool

from app.core.errors import DomainError
from app.modules.cliente_experiencia_compra.shared.models.comercio import Pago, Venta
from app.modules.cliente_experiencia_compra.cu14_pago_electronico.services.checkout_stripe import reconcile

router = APIRouter(prefix="/api/integraciones/stripe", tags=["Stripe"])


def apply_event(request, event, stripe):
    session = event.get("data", {}).get("object", {})
    raw_id = session.get("metadata", {}).get("idPago", "")
    if not isinstance(raw_id, str) or not raw_id.isdigit():
        return
    with request.app.state.session_factory() as db:
        pago = db.get(Pago, int(raw_id))
        venta = db.get(Venta, pago.nroventa) if pago else None
        if not venta or not pago.referencia:
            return
        reference = pago.referencia.removeprefix("refund:")
        if reference != session.get("id"):
            return
        user_id = venta.idusuariocl
        db.rollback()
        reconcile(db, int(raw_id), user_id, None, stripe)


@router.post("/webhook")
async def webhook(request: Request):
    stripe = getattr(request.app.state, "stripe", None)
    if stripe is None:
        raise DomainError(503, "pasarela_no_disponible", "Stripe no está configurado")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 1_000_000:
            raise DomainError(413, "evento_invalido", "Evento demasiado grande")
    try:
        event = stripe.verify_event(bytes(body), request.headers.get("stripe-signature", ""))
    except (ValueError, TypeError, KeyError):
        raise DomainError(400, "firma_invalida", "Evento Stripe no válido") from None
    if event.get("type") in {"checkout.session.completed", "checkout.session.expired",
                             "checkout.session.async_payment_succeeded", "checkout.session.async_payment_failed"}:
        try:
            await run_in_threadpool(apply_event, request, event, stripe)
        except RuntimeError:
            raise DomainError(503, "pasarela_no_disponible", "Reintente el evento posteriormente") from None
    return {"recibido": True}
