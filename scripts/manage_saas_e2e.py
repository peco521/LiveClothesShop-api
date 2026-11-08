"""Snapshot and restore the exact SaaS demo state modified by the browser E2E."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT.parent / ".local" / "backend.env"


def engine():
    if not ENV_FILE.is_file():
        raise SystemExit("Falta .local/backend.env para fashionstore_test")
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
        raise SystemExit("El E2E SaaS solo permite el rol de plataforma en fashionstore_test local")
    return create_engine(url, hide_parameters=True)


def snapshot(connection, code):
    company = connection.execute(text("""
        SELECT idEmp, nombre, logoUrl, colorPrimario, colorSecundario
        FROM empresa WHERE codigo=:codigo
    """), {"codigo": code}).mappings().one()
    subscriptions = connection.execute(text("""
        SELECT idSusc, idPlan, fechaInicio, fechaVencimiento, estado
        FROM suscripcion WHERE idEmp=:idemp ORDER BY idSusc
    """), {"idemp": company["idemp"]}).mappings()
    actor = connection.execute(text(
        "SELECT idUsuario FROM usuario WHERE tipo='S' AND idEmp IS NULL LIMIT 1")).scalar_one()
    return {
        "idEmp": company["idemp"],
        "company": {
            key: company[key] for key in ("nombre", "logourl", "colorprimario", "colorsecundario")
        },
        "subscriptions": [{
            "idSusc": row["idsusc"], "idPlan": row["idplan"],
            "fechaInicio": row["fechainicio"].isoformat(),
            "fechaVencimiento": row["fechavencimiento"].isoformat(),
            "estado": row["estado"],
        } for row in subscriptions],
        "auditMax": connection.execute(
            text("SELECT COALESCE(max(id), 0) FROM bitacora")).scalar_one(),
        "actor": actor,
    }


def restore(connection, state):
    idemp = state["idEmp"]
    initial = state["subscriptions"]
    initial_ids = [row["idSusc"] for row in initial]
    current_ids = connection.execute(text(
        "SELECT idSusc FROM suscripcion WHERE idEmp=:idemp"),
        {"idemp": idemp}).scalars().all()
    added = [value for value in current_ids if value not in initial_ids]
    if added:
        connection.execute(text(
            "DELETE FROM suscripcion_plan_version WHERE idSusc = ANY(CAST(:ids AS integer[]))"),
            {"ids": added})
        connection.execute(text(
            "DELETE FROM suscripcion WHERE idSusc = ANY(CAST(:ids AS integer[]))"),
            {"ids": added})
    for subscription in initial:
        connection.execute(text("""
            UPDATE suscripcion
            SET idPlan=:plan, fechaInicio=:inicio, fechaVencimiento=:fin, estado=:estado
            WHERE idSusc=:idsusc AND idEmp=:idemp
        """), {"plan": subscription["idPlan"], "inicio": subscription["fechaInicio"],
              "fin": subscription["fechaVencimiento"], "estado": subscription["estado"],
              "idsusc": subscription["idSusc"], "idemp": idemp})
    company = state["company"]
    connection.execute(text("""
        UPDATE empresa
        SET nombre=:nombre, logoUrl=:logo, colorPrimario=:primario, colorSecundario=:secundario
        WHERE idEmp=:idemp
    """), {"nombre": company["nombre"], "logo": company["logourl"],
          "primario": company["colorprimario"], "secundario": company["colorsecundario"],
          "idemp": idemp})
    connection.execute(text("""
        DELETE FROM bitacora
        WHERE id > :audit_max AND usuario_id=:actor
          AND accion IN ('empresa_identidad_actualizada', 'empresa_plan_cambiado',
                         'soporte_acceso_empresa')
          AND detalles->>'idEmp'=:idemp
    """), {"audit_max": state["auditMax"], "actor": state["actor"], "idemp": str(idemp)})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode", required=True)
    save = subparsers.add_parser("snapshot")
    save.add_argument("--company", default="manaco")
    restore_parser = subparsers.add_parser("restore")
    restore_parser.add_argument("--snapshot", required=True)
    args = parser.parse_args()
    database = engine()
    try:
        if args.mode == "snapshot":
            with database.connect() as connection:
                print(json.dumps(snapshot(connection, args.company), separators=(",", ":")))
        else:
            state = json.loads(args.snapshot)
            with database.begin() as connection:
                restore(connection, state)
    finally:
        database.dispose()


if __name__ == "__main__":
    main()
