"""Remove only the uniquely named CU05 Playwright employee from test DB."""

import argparse
import re
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT.parent / ".local" / "backend.env"
EMAIL_PATTERN = re.compile(r"^e2e-cu05-[0-9]+@example\.com$")


def test_url() -> str:
    values = {}
    for line in ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    url = values.get("TEST_PLATFORM_DATABASE_URL", "")
    parsed = make_url(url)
    if (parsed.host != "127.0.0.1" or parsed.port != 55432
            or parsed.database != "fashionstore_test"
            or parsed.username != "fashionstore_provisioner"):
        raise SystemExit("La limpieza E2E solo permite fashionstore_test en 127.0.0.1:55432")
    return url


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--empresa", required=True, choices=(
        "manaco", "kosy-pantalones", "urbanwomen", "kosy-prendas", "lupo"))
    parser.add_argument("--correo", required=True)
    args = parser.parse_args()
    if not EMAIL_PATTERN.fullmatch(args.correo):
        raise SystemExit("Solo se permite limpiar una cuenta temporal CU05 de Playwright")

    engine = create_engine(test_url(), hide_parameters=True)
    try:
        with engine.begin() as connection:
            connection.execute(text("""
                DELETE FROM usuario u USING empresa e
                WHERE u.idEmp=e.idEmp AND e.codigo=:empresa
                  AND lower(btrim(u.correo))=:correo AND u.tipo='E'
            """), {"empresa": args.empresa, "correo": args.correo.lower()})
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
