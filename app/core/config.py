from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from app.integrations.payments.modos import (
    MONEDAS_ADMITIDAS,
    PREFIJO_FIRMA,
    ModoStripe,
    clave_corresponde,
    modo_por_environment,
)


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
    # Rol de base de datos para el panel SaaS y el aprovisionamiento: debe tener
    # BYPASSRLS y NO se usa nunca en las rutas HTTP de empresa. Si falta, las
    # rutas /api/saas responden 503 plataforma_no_configurada.
    platform_database_url: SecretStr | None = None
    # Uso explícito de DATABASE_URL para plataforma, solo en desarrollo local.
    # Comparte credenciales con el motor empresarial y no brinda aislamiento por rol.
    platform_use_database_url: bool = False
    backups_directory: str = str(Path(__file__).resolve().parents[2] / '.local' / 'saas-backups')
    backups_pg_dump: str = ''
    backups_timeout_seconds: int = Field(default=1800, ge=30, le=14400)
    # Modo demostracion: habilita el endpoint de credenciales de prueba y la
    # carga de tenants de demostracion. Prohibido en produccion.
    demo_mode: bool = False
    demo_credentials_file: str = ""
    # Lista explicita (CSV) de modulos empresariales ya aislados y verificados.
    # Todo lo que no figure aqui sigue devolviendo 503 modulo_pendiente.
    multitenant_modules: str = ""
    # Motor de sincronizacion offline.
    sync_max_operations: int = Field(default=50, ge=1, le=200)
    sync_stale_minutes: int = Field(default=5, ge=1, le=60)
    cliente_rol_id: str = Field(min_length=1, max_length=15)
    session_hours: int = Field(default=8, ge=1, le=168)
    environment: Literal["development", "production", "test"] = "development"
    multitenant_enabled: bool = True
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
    # CU25: `fechahora` se guarda en UTC (`func.now()`), así que el dashboard y los
    # reportes agrupan por el día/mes de la zona horaria del negocio.
    reportes_zona_horaria: str = "America/La_Paz"
    # Optional official local Qwen GGUF. No external paid provider is contacted.
    reportes_ia_local_enabled: bool = False
    # CU09: validación de direcciones contra OpenStreetMap/Nominatim (sin claves).
    # "disabled" (defecto) no valida y deja latitud/longitud en NULL.
    geocoding_provider: Literal["disabled", "nominatim"] = "disabled"
    geocoding_base_url: str = "https://nominatim.openstreetmap.org"
    geocoding_timeout: float = Field(default=5, gt=0, le=30)
    geocoding_user_agent: str = Field(default="LiveClothesShop/1.0 (proyecto academico)", max_length=150)
    allowed_origins: list[str] = Field(default_factory=lambda: [
        "http://localhost:4200", "http://localhost:4300",
    ])

    @model_validator(mode="after")
    def validate_security(self):
        if self.platform_use_database_url and self.environment != "development":
            raise ValueError("PLATFORM_USE_DATABASE_URL solo está permitido en desarrollo")
        if self.platform_use_database_url and self.platform_database_url is not None:
            raise ValueError("Configure PLATFORM_DATABASE_URL o PLATFORM_USE_DATABASE_URL, no ambos")
        if self.environment == "production" and not self.multitenant_enabled:
            raise ValueError("MULTITENANT_ENABLED es obligatorio en producción")
        if self.environment == "production" and self.demo_mode:
            raise ValueError("DEMO_MODE solo está permitido en desarrollo o pruebas")
        if self.platform_database_url is not None:
            valor = self.platform_database_url.get_secret_value()
            if not valor.startswith("postgresql+psycopg://"):
                raise ValueError("PLATFORM_DATABASE_URL debe utilizar PostgreSQL con Psycopg 3")
            try:
                if not make_url(valor).database:
                    raise ValueError
            except (ArgumentError, ValueError):
                raise ValueError("PLATFORM_DATABASE_URL no tiene un formato válido") from None
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
        if self.geocoding_provider == "nominatim":
            # Nominatim exige User-Agent identificable y una URL base limpia.
            parsed = urlsplit(self.geocoding_base_url)
            if (parsed.scheme not in {"http", "https"} or not parsed.netloc
                    or parsed.query or parsed.fragment or parsed.username or parsed.password):
                raise ValueError("GEOCODING_BASE_URL debe ser una URL base http(s) sin credenciales ni parámetros")
            if not self.geocoding_user_agent.strip():
                raise ValueError("GEOCODING_USER_AGENT es obligatorio para usar Nominatim")
        return self

    @property
    def enabled_tenant_modules(self) -> frozenset[str]:
        """Modulos empresariales ya aislados por empresa y verificados.

        Se declara de forma explicita, no por ausencia de bloqueo: un modulo que
        no figure aqui devuelve 503 en multitenant.
        """
        return frozenset(part.strip() for part in self.multitenant_modules.split(",") if part.strip())

    @property
    def stripe_mode(self) -> ModoStripe:
        """Modo Stripe derivado de ENVIRONMENT.

        `development`/`test` → Stripe TEST; `production` → Stripe LIVE. Se pasa a
        la pasarela desde aquí, así ningún módulo de cobro decide el entorno y el
        mismo código sirve en los dos escenarios cambiando solo la configuración.
        """
        return modo_por_environment(self.environment)

    @model_validator(mode="after")
    def validate_payments_mode(self):
        """Impide mezclar entornos con el modo Stripe equivocado.

        Una clave LIVE en desarrollo o pruebas se rechaza: evita cobros reales por
        un error de configuración local. Una clave de PRUEBA en producción también
        se rechaza: evita desplegar sin cobrar. Los prefijos admitidos viven solo en
        `app/integrations/payments/modos.py` y el modo se deriva de `ENVIRONMENT`.
        Los mensajes nunca incluyen el valor de las claves.
        """
        if self.payments_provider == "stripe":
            if not clave_corresponde(self.stripe_secret_key.get_secret_value(), self.stripe_mode):
                raise ValueError("STRIPE_SECRET_KEY no corresponde al modo Stripe del entorno configurado")
            if not self.stripe_webhook_secret.get_secret_value().startswith(PREFIJO_FIRMA):
                raise ValueError("STRIPE_WEBHOOK_SECRET debe ser un secreto de firma de webhook")
            if self.stripe_currency not in MONEDAS_ADMITIDAS:
                raise ValueError("STRIPE_CURRENCY debe ser una moneda admitida: usd, eur o bob")
        return self
