"""Run the real API for browser E2E against the isolated PostgreSQL test DB only."""

from pathlib import Path
import argparse
import sys

from sqlalchemy.engine import make_url
from pydantic import SecretStr
import uvicorn


ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT.parent / ".local" / "backend.env"
sys.path.insert(0, str(ROOT))

from app.core.config import Settings
from app.main import create_app


def read_test_urls() -> tuple[str, str]:
    if not ENV_FILE.is_file():
        raise SystemExit("Falta el archivo local de configuración PostgreSQL de pruebas")
    values = {}
    for line in ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    app_url = values.get("TEST_DATABASE_URL", "")
    platform_url = values.get("TEST_PLATFORM_DATABASE_URL", "")
    for value in (app_url, platform_url):
        parsed = make_url(value)
        if (parsed.host != "127.0.0.1" or parsed.port != 55432
                or parsed.database != "fashionstore_test"):
            raise SystemExit("El servidor E2E solo permite 127.0.0.1:55432/fashionstore_test")
    if (make_url(app_url).username != "fashionstore_app"
            or make_url(platform_url).username != "fashionstore_provisioner"):
        raise SystemExit("La API de pruebas requiere los roles HTTP y plataforma diferenciados")
    return app_url, platform_url


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--host", choices=("127.0.0.1", "0.0.0.0"), default="127.0.0.1",
        help="Use 0.0.0.0 only for a trusted Android emulator/device on the local test network.")
    args = parser.parse_args()
    app_url, platform_url = read_test_urls()
    settings = Settings(
        database_url=SecretStr(app_url),
        platform_database_url=SecretStr(platform_url),
        cliente_rol_id="E5-CLI",
        environment="test",
        multitenant_enabled=True,
        # Explicit local verification selection. Cash sales use atomic /api/sync;
        # digital checkout uses CU13/CU14 with non-production mock only.
        multitenant_modules="cu05,cu06,cu07,cu08,cu09,cu10,cu11,cu12,cu13,cu14,cu15,cu18,cu19,cu20,cu21,cu22,cu23,cu25,sync,saas,cu24",
        reportes_ia_local_enabled=(ROOT / '.local/models/qwen2.5-1.5b-instruct-q4_k_m.gguf').is_file(),
        demo_mode=True,
        password_storage="argon2id",
        payments_provider="mock",
        recovery_delivery="disabled",
        geocoding_provider="disabled",
        allowed_origins=["http://localhost:4200", "http://127.0.0.1:4200"],
    )
    uvicorn.run(create_app(settings), host=args.host, port=8000,
                log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
