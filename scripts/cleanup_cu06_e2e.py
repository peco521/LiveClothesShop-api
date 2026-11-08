"""Remove only a uniquely named CU06 Playwright role from the test DB."""

import argparse
import re
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT.parent / ".local" / "backend.env"
ROLE_PATTERN = re.compile(r"^E2E6-[0-9]{10}$")


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
    parser.add_argument("--rol", required=True)
    args = parser.parse_args()
    if not ROLE_PATTERN.fullmatch(args.rol):
        raise SystemExit("Solo se permite limpiar un identificador temporal CU06 de Playwright")

    engine = create_engine(test_url(), hide_parameters=True)
    try:
        with engine.begin() as connection:
            connection.execute(text("""
                DELETE FROM rol_funcion rf USING empresa e
                WHERE rf.idEmp=e.idEmp AND e.codigo=:empresa AND rf.nroRol=:rol
            """), {"empresa": args.empresa, "rol": args.rol})
            connection.execute(text("""
                DELETE FROM rol r USING empresa e
                WHERE r.idEmp=e.idEmp AND e.codigo=:empresa AND r.nro=:rol
            """), {"empresa": args.empresa, "rol": args.rol})
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
