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
    cookie_secure: bool = False
    cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    cookie_name: str = Field(default="liveclothes_session", pattern=r"^[A-Za-z0-9_-]{1,64}$")
    allowed_origins: list[str] = Field(default_factory=lambda: [
        "http://localhost:4200", "http://localhost:4300",
    ])

    @model_validator(mode="after")
    def validate_security(self):
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
        return self
