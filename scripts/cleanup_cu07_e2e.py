"""Remove only a uniquely named CU07 Playwright client from the test database."""

import argparse
import re
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT.parent / ".local" / "backend.env"
EMAIL_PATTERN = re.compile(r"^e2e-cu07-[0-9]+@example\.com$")
COMPANIES = ("manaco", "kosy-pantalones", "urbanwomen", "kosy-prendas", "lupo")


def test_url() -> str:
    if not ENV_FILE.is_file():
        raise SystemExit("Falta la configuración local de PostgreSQL de pruebas")
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
    parser.add_argument("--empresa", required=True, choices=COMPANIES)
    parser.add_argument("--correo", required=True)
    args = parser.parse_args()
    if not EMAIL_PATTERN.fullmatch(args.correo):
        raise SystemExit("Solo se permite limpiar una cuenta temporal CU07 de Playwright")

    engine = create_engine(test_url(), hide_parameters=True)
    try:
        with engine.begin() as connection:
            users = connection.execute(text("""
                SELECT u.idEmp AS "idEmp", u.idUsuario AS "idUsuario"
                FROM usuario u JOIN empresa e ON e.idEmp=u.idEmp
                WHERE e.codigo=:empresa AND u.tipo='C'
                  AND lower(btrim(u.correo))=:correo
            """), {"empresa": args.empresa, "correo": args.correo.lower()}).mappings().all()
            if len(users) > 1:
                raise SystemExit("La cuenta temporal no es única; no se eliminó ningún registro")
            for user in users:
                connection.execute(text("""
                    DELETE FROM bitacora
                    WHERE idEmp=:idemp
                      AND (detalles->>'idUsuario'=:user_id OR usuario_id=:user_id)
                """), {"idemp": user["idEmp"], "user_id": user["idUsuario"]})
                connection.execute(text("""
                    DELETE FROM recuperacion_contrasena
                    WHERE idEmp=:idemp AND usuario_id=:user_id
                """), {"idemp": user["idEmp"], "user_id": user["idUsuario"]})
                connection.execute(text("""
                    DELETE FROM cliente WHERE idEmp=:idemp AND idUsuario=:user_id
                """), {"idemp": user["idEmp"], "user_id": user["idUsuario"]})
                connection.execute(text("""
                    DELETE FROM usuario WHERE idEmp=:idemp AND idUsuario=:user_id
                """), {"idemp": user["idEmp"], "user_id": user["idUsuario"]})
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
