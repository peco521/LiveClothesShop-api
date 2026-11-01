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
        # CU05/CU06 admin modules and CU24 caja reads are tenant-scoped;
        # sales still go through atomic /api/sync.
        multitenant_modules="cu05,cu06,cu09,sync,saas,cu24",
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
