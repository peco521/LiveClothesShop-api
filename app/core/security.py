import hashlib
import secrets
from datetime import datetime, timezone

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError


class Passwords:
    def __init__(self):
        self.hasher = PasswordHasher(type=Type.ID)
        # Equal-cost verification for absent users and unsupported legacy hashes.
        self._dummy = self.hasher.hash(secrets.token_urlsafe(32))

    def hash(self, password: str) -> str:
        return self.hasher.hash(password)

    def verify(self, encoded: str | None, password: str) -> bool:
        supported = bool(encoded and encoded.startswith("$argon2id$"))
        try:
            valid = self.hasher.verify(encoded if supported else self._dummy, password)
            return bool(valid and supported)
        except (VerificationError, InvalidHashError):
            return False


def new_credential() -> str:
    return secrets.token_urlsafe(32)


def digest(credential: str) -> str:
    return hashlib.sha256(credential.encode("utf-8")).hexdigest()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
