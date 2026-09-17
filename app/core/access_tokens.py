"""Accesos opacos en memoria: una instancia de API, sin tabla de sesiones.

El cliente conserva el token; aquí solo se guarda su digest. Reiniciar el
proceso invalida todos los accesos. No usar múltiples workers o réplicas.
"""
from dataclasses import dataclass
from datetime import datetime
from threading import RLock

from app.core.errors import DomainError
from app.core.security import digest, new_credential, utcnow


@dataclass(frozen=True)
class Access:
    usuario_id: str
    password_digest: str
    expira_en: datetime


class AccessTokens:
    """Thread-safe, process-local access store; never persisted to the database."""

    def __init__(self, *, capacity: int = 10000):
        self._entries: dict[str, Access] = {}
        self._lock = RLock()
        self.capacity = capacity

    def issue(self, usuario_id: str, password_hash: str, expires: datetime) -> str:
        with self._lock:
            now = utcnow()
            self._entries = {
                key: value for key, value in self._entries.items() if value.expira_en > now
            }
            if len(self._entries) >= self.capacity:
                raise DomainError(503, "accesos_no_disponibles", "No se pudo iniciar sesión. Inténtalo más tarde")
            credential = new_credential()
            self._entries[digest(credential)] = Access(usuario_id, digest(password_hash), expires)
            return credential

    def get(self, credential: str) -> Access | None:
        with self._lock:
            key = digest(credential)
            value = self._entries.get(key)
            if value is not None and value.expira_en <= utcnow():
                self._entries.pop(key, None)
                return None
            return value

    def revoke(self, credential: str) -> None:
        with self._lock:
            self._entries.pop(digest(credential), None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
