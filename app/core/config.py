from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Local development file; environment variables retain precedence.
        env_file=Path(__file__).resolve().parents[2] / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        hide_input_in_errors=True,
    )

    database_url: SecretStr
    cliente_rol_id: str = Field(min_length=1, max_length=15)
    session_hours: int = Field(default=8, ge=1, le=168)
    environment: Literal["development", "production", "test"] = "development"
    password_storage: Literal["argon2id", "plaintext"] = "argon2id"
    cookie_secure: bool = False
    cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    cookie_name: str = Field(default="liveclothes_session", pattern=r"^[A-Za-z0-9_-]{1,64}$")
    recovery_delivery: Literal["disabled", "gmail"] = "disabled"
    recovery_frontend_url: str = "http://localhost:4200"
    gmail_client_id: SecretStr = SecretStr("")
    gmail_client_secret: SecretStr = SecretStr("")
    gmail_refresh_token: SecretStr = SecretStr("")
    gmail_sender: str = Field(default="", max_length=254)
    payments_provider: Literal["mock", "stripe"] = "mock"
    stripe_secret_key: SecretStr = SecretStr("")
    stripe_webhook_secret: SecretStr = SecretStr("")
    stripe_currency: str = ""
    cloudinary_cloud_name: str = Field(default="", pattern=r"^[A-Za-z0-9_-]*$")
    cloudinary_api_key: SecretStr = SecretStr("")
    cloudinary_api_secret: SecretStr = SecretStr("")
    payments_frontend_url: str = "http://localhost:4200"
    allowed_origins: list[str] = Field(default_factory=lambda: [
        "http://localhost:4200", "http://localhost:4300",
    ])

    @model_validator(mode="after")
    def validate_security(self):
        if self.environment == "production" and self.password_storage == "plaintext":
            raise ValueError("PASSWORD_STORAGE=plaintext solo está permitido en desarrollo o pruebas")
        if not self.database_url.get_secret_value().startswith("postgresql+psycopg://"):
            raise ValueError("DATABASE_URL debe utilizar PostgreSQL con Psycopg 3")
        try:
            url = make_url(self.database_url.get_secret_value())
            if not url.database:
                raise ValueError
        except (ArgumentError, ValueError):
            raise ValueError("DATABASE_URL no tiene un formato válido") from None
        if self.environment == "production" and not self.cookie_secure:
            raise ValueError("COOKIE_SECURE es obligatorio en producción")
        if self.cookie_samesite == "none" and not self.cookie_secure:
            raise ValueError("SameSite=None requiere COOKIE_SECURE")
        for origin in self.allowed_origins:
            parsed = urlsplit(origin)
            if (parsed.scheme not in {"http", "https"} or not parsed.netloc
                    or parsed.path or parsed.query or parsed.fragment
                    or parsed.username or parsed.password):
                raise ValueError("ALLOWED_ORIGINS debe contener orígenes exactos")
            if self.environment == "production" and parsed.scheme != "https":
                raise ValueError("Los orígenes de producción deben utilizar HTTPS")
        if self.recovery_delivery == "gmail":
            if self.recovery_frontend_url not in self.allowed_origins:
                raise ValueError("RECOVERY_FRONTEND_URL debe ser un origen incluido en ALLOWED_ORIGINS")
            if self.gmail_sender and ("@" not in self.gmail_sender or any(
                    char in self.gmail_sender for char in "\r\n")):
                raise ValueError("GMAIL_SENDER debe ser un correo válido sin saltos de línea")
        if self.payments_provider == "stripe" and self.payments_frontend_url not in self.allowed_origins:
            raise ValueError("PAYMENTS_FRONTEND_URL debe estar incluido en ALLOWED_ORIGINS")
        return self
