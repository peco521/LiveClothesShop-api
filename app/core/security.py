import hashlib
import re
import secrets
from datetime import datetime, timezone

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError


class Passwords:
    def __init__(self, storage: str = "argon2id"):
        if storage not in {"argon2id", "plaintext"}:
            raise ValueError("Modo de almacenamiento de contraseña inválido")
        self.storage = storage
        self.hasher = PasswordHasher(type=Type.ID)
        # Equal-cost verification for absent users and unsupported legacy hashes.
        self._dummy = self.hasher.hash(secrets.token_urlsafe(32))

    def hash(self, password: str) -> str:
        # Explicit classroom-only mode. Never expose this value in API responses.
        if self.storage == "plaintext":
            return password
        return self.hasher.hash(password)

    def verify(self, encoded: str | None, password: str) -> bool:
        supported = bool(encoded and encoded.startswith("$argon2id$"))
        if self.storage == "plaintext" and not supported:
            return bool(encoded and secrets.compare_digest(digest(encoded), digest(password)))
        try:
            valid = self.hasher.verify(encoded if supported else self._dummy, password)
            return bool(valid and supported)
        except (VerificationError, InvalidHashError):
            return False

    def verify_for_login(self, stored: str | None, password: str) -> tuple[bool, bool]:
        """Return (valid, migrate_storage); only successful login may convert storage.

        Recognizable hashes must never be accepted as literal passwords.
        In the default Argon2id mode, unsupported encoded formats require recovery.
        """
        valid = self.verify(stored, password)
        if stored and stored.startswith("$argon2id$"):
            return valid, valid and self.storage == "plaintext"
        if self.storage == "plaintext":
            return valid, False
        if (not stored or not stored.strip() or len(stored) > 128
                or re.match(r"^(?:\$|\{|(?:pbkdf2|scrypt|bcrypt|argon2|sha\d*|md5)[_$:])", stored, re.I)
                or re.fullmatch(r"[0-9a-fA-F]{32,128}", stored)):
            return False, False
        # Fixed-length digests support Unicode and avoid comparing plaintext
        # with ordinary string equality. verify() already did dummy Argon2 work.
        valid = secrets.compare_digest(digest(stored), digest(password))
        return valid, valid


def new_credential() -> str:
    return secrets.token_urlsafe(32)


def digest(credential: str) -> str:
    return hashlib.sha256(credential.encode("utf-8")).hexdigest()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
