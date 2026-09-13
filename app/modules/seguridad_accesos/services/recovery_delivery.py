from datetime import datetime
from typing import Protocol

from fastapi import Request


class RecoveryDelivery(Protocol):
    """Trusted injected adapter, never an HTTP endpoint.

    dispatch must have comparable latency for a recipient and None (dummy work),
    must not log/persist plaintext tokens, and must raise on delivery failure.
    Availability must be global, not recipient-dependent; implementations must
    not expose recipient rejection/bounce results through this synchronous API.
    Links must use /restablecer-contrasena#token=<token>, never a query string.
    """

    def dispatch(self, recipient: str | None, token: str, expires: datetime) -> None: ...


def get_recovery_delivery(request: Request) -> RecoveryDelivery | None:
    return getattr(request.app.state, "recovery_delivery", None)
