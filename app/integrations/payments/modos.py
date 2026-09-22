"""Modo de Stripe (prueba/vivo) resuelto únicamente por configuración.

El MISMO código sirve en desarrollo/pruebas (Stripe TEST) y en producción
(Stripe LIVE): el modo se deriva de `ENVIRONMENT` y se pasa a la pasarela porque
ninguna capa de cobro decide el entorno por su cuenta.

Aquí viven, centralizados, los prefijos y expectativas de cada modo para que no
se repitan en el resto del proyecto:

| Modo | Clave secreta | Sesión de Checkout | Eventos de webhook |
|---|---|---|---|
| `test` | `sk_test_...` | `cs_test_...` | `livemode=false` |
| `live` | `sk_live_...` | `cs_live_...` | `livemode=true` |

`ENVIRONMENT=development` (o `test`) exige claves de prueba; `production` exige
claves live. La combinación contraria se rechaza al construir `Settings`, antes
de cualquier llamada de red.
"""
from typing import Literal

ModoStripe = Literal["test", "live"]

# Secreto de firma de webhook: el mismo prefijo en local (Stripe CLI) y en el
# endpoint desplegado, pero con valores distintos e independientes entre sí.
PREFIJO_FIRMA = "whsec_"
PREFIJOS_CLAVE: dict[str, str] = {"test": "sk_test_", "live": "sk_live_"}
PREFIJOS_SESION: dict[str, str] = {"test": "cs_test_", "live": "cs_live_"}
LIVEMODE: dict[str, bool] = {"test": False, "live": True}
MONEDAS_ADMITIDAS = frozenset({"usd", "eur", "bob"})


def modo_por_environment(environment: str) -> ModoStripe:
    """`production` cobra en vivo; desarrollo y pruebas usan Stripe TEST."""
    return "live" if environment == "production" else "test"


def modo_de_adaptador(adaptador) -> ModoStripe:
    """Modo declarado por la pasarela inyectada (por defecto, prueba)."""
    modo = getattr(adaptador, "modo", "test")
    return modo if modo in PREFIJOS_CLAVE else "test"


def clave_corresponde(clave: str, modo: ModoStripe) -> bool:
    """La clave secreta existe y pertenece al modo solicitado."""
    return bool(clave) and clave.startswith(PREFIJOS_CLAVE[modo])


def sesion_corresponde(referencia, modo: ModoStripe) -> bool:
    """La referencia es una sesión de Checkout del modo solicitado."""
    return isinstance(referencia, str) and referencia.startswith(PREFIJOS_SESION[modo])


def es_sesion_de_pasarela(referencia) -> bool:
    """Reconoce una sesión de Checkout de cualquier modo.

    Se usa solo para decidir que una referencia proviene de Stripe (p. ej. al
    cancelar un pago pendiente); el modo lo valida después el adaptador.
    """
    return isinstance(referencia, str) and any(
        referencia.startswith(prefijo) for prefijo in PREFIJOS_SESION.values())


def livemode_esperado(modo: ModoStripe) -> bool:
    """Valor de `livemode` que deben traer las sesiones y eventos del modo."""
    return LIVEMODE[modo]
