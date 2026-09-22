"""Stripe hosted Checkout en el modo que indique la configuración. No PAN/CVC enters this API."""
import hashlib
import hmac
import json
import time
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from app.integrations.payments.modos import (
    MONEDAS_ADMITIDAS,
    PREFIJO_FIRMA,
    PREFIJOS_CLAVE,
    clave_corresponde,
    livemode_esperado,
    sesion_corresponde,
)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class StripeCheckout:
    """Adaptador de Checkout para Stripe TEST y Stripe LIVE.

    El modo llega desde `Settings.stripe_mode` (development/test → "test",
    production → "live"); aquí no se decide el entorno, solo se comprueba que la
    clave secreta, las sesiones y los eventos correspondan al modo declarado.
    """

    def __init__(self, secret, webhook_secret, currency, frontend_url, modo="test"):
        if modo not in PREFIJOS_CLAVE:
            raise ValueError("Modo Stripe inválido: use test o live")
        if not clave_corresponde(secret, modo) or not webhook_secret.startswith(PREFIJO_FIRMA):
            raise ValueError("Stripe requiere claves y secreto de webhook del modo configurado")
        # Deliberately support only reviewed two-decimal currencies; no implicit FX.
        if currency not in MONEDAS_ADMITIDAS:
            raise ValueError("Configure una moneda admitida: usd, eur o bob")
        self.modo = modo
        self.livemode = livemode_esperado(modo)
        self.secret = secret
        self.webhook_secret = webhook_secret
        self.currency = currency
        self.frontend_url = frontend_url.rstrip("/")

    def request(self, path, params=None, key=None):
        headers = {"Authorization": "Bearer " + self.secret}
        if key:
            headers["Idempotency-Key"] = key
        body = None if params is None else urlencode(params).encode()
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = Request("https://api.stripe.com/v1/" + path, data=body, headers=headers,
                      method="GET" if body is None else "POST")
        try:
            with build_opener(NoRedirect()).open(req, timeout=15) as response:
                return json.loads(response.read(1_000_000))
        except Exception:
            raise RuntimeError("Stripe no está disponible; consulte el estado antes de reintentar") from None

    def create(self, id_pago, nro_venta, monto, success_url=None, cancel_url=None):
        amount = monto * 100
        if amount <= 0 or amount != amount.to_integral_value():
            raise ValueError("Monto inválido para Stripe")
        session = self.request("checkout/sessions", {
            "mode": "payment", "payment_method_types[0]": "card",
            "line_items[0][price_data][currency]": self.currency,
            "line_items[0][price_data][unit_amount]": int(amount),
            "line_items[0][price_data][product_data][name]": f"Compra LiveClothesShop {nro_venta}",
            "line_items[0][quantity]": 1,
            "metadata[idPago]": id_pago, "metadata[nroVenta]": nro_venta,
            # El llamador decide a dónde vuelve el navegador: la tienda del cliente
            # (CU13/CU14) o el comprobante administrativo de caja (CU24).
            "success_url": success_url or f"{self.frontend_url}/tienda/pago/{id_pago}",
            "cancel_url": cancel_url or f"{self.frontend_url}/tienda/pago/{id_pago}?cancelar=1",
        }, key=f"lcs-checkout-{id_pago}")
        # Defensa en profundidad: si la respuesta no pertenece al modo configurado,
        # no se guarda como referencia de un pago local.
        if not sesion_corresponde(session.get("id"), self.modo):
            raise ValueError("Stripe devolvió una sesión que no corresponde al modo configurado")
        return session

    def retrieve(self, session):
        if not sesion_corresponde(session, self.modo):
            raise ValueError("Sesión Stripe inválida")
        return self.request("checkout/sessions/" + quote(session, safe=""))

    def expire(self, session):
        return self.request("checkout/sessions/" + quote(session, safe="") + "/expire", {},
                            key="lcs-expire-" + session)

    def refund(self, session, id_pago):
        intent = session.get("payment_intent")
        if not isinstance(intent, str) or not intent.startswith("pi_"):
            raise ValueError("Pago Stripe sin referencia verificable")
        return self.request("refunds", {"payment_intent": intent}, key=f"lcs-refund-{id_pago}")

    def refunded(self, session):
        intent = session.get("payment_intent")
        if not isinstance(intent, str) or not intent.startswith("pi_"):
            raise ValueError("Pago Stripe sin referencia verificable")
        refunds = self.request("refunds?" + urlencode({"payment_intent": intent, "limit": 100}))
        # Pending or failed refunds must never be reported as completed.
        amount = sum(item["amount"] for item in refunds["data"]
                     if item.get("status") == "succeeded")
        return amount >= session["amount_total"]

    def refund_partial(self, session, amount, id_refund):
        intent = session.get('payment_intent')
        if not isinstance(intent, str) or not intent.startswith('pi_'):
            raise ValueError('Pago Stripe sin referencia verificable')
        cents = amount * 100
        if cents <= 0 or cents != cents.to_integral_value():
            raise ValueError('Monto inválido para reembolso')
        return self.request('refunds', {'payment_intent': intent, 'amount': int(cents)},
                            key=f'lcs-return-refund-{id_refund}')

    def verify_event(self, payload, signature):
        if len(payload) > 1_000_000:
            raise ValueError("Evento inválido")
        parts = [part.split("=", 1) for part in signature.split(",") if "=" in part]
        timestamp = next((value for name, value in parts if name == "t"), "")
        if not timestamp.isdigit() or abs(time.time() - int(timestamp)) > 300:
            raise ValueError("Firma inválida")
        expected = hmac.new(self.webhook_secret.encode(), timestamp.encode() + b"." + payload,
                            hashlib.sha256).hexdigest()
        if not any(name == "v1" and hmac.compare_digest(expected, value) for name, value in parts):
            raise ValueError("Firma inválida")
        event = json.loads(payload)
        if not isinstance(event, dict):
            raise ValueError("Evento inválido")
        if event.get("livemode") is not self.livemode:
            # development/test exige livemode=false; production exige livemode=true.
            raise ValueError("El evento no corresponde al modo Stripe configurado")
        return event
