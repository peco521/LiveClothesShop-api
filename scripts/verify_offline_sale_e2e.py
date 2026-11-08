"""Fixture and read-only assertions for the Chrome CU24 offline-sale E2E.

Only connects to 127.0.0.1:55432/fashionstore_test using the local platform URL.
No credentials or connection strings are printed.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from uuid import UUID

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
        raise SystemExit("El E2E solo permite 127.0.0.1:55432/fashionstore_test")
    return create_engine(url, hide_parameters=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("fixture", "snapshot", "cleanup"))
    parser.add_argument("--uuid", type=UUID)
    parser.add_argument("--cleanup-uuid", type=UUID, action="append", default=[])
    parser.add_argument("--variant")
    parser.add_argument("--branch", type=int)
    parser.add_argument("--stock-before", type=int)
    args = parser.parse_args()
    db = engine()
    try:
        with db.connect() as conn:
            if args.mode == "fixture":
                row = conn.execute(text("""
                    SELECT e.idEmp AS idemp, em.idUsuario AS user_id, s.nro AS branch,
                           p.idProd AS product_id, p.descripcion AS description,
                           v.idVariante AS variant, v.precio AS price, i.cantDisp AS stock
                    FROM empresa e
                    JOIN empleado em ON em.idEmp=e.idEmp AND em.cargo='Cajero'
                    JOIN sucursal s ON s.idEmp=e.idEmp AND s.nro=em.nroSuc AND s.estado='activo'
                    JOIN inventario i ON i.idEmp=s.idEmp AND i.nroSuc=s.nro AND i.cantDisp>0
                    JOIN varianteProd v ON v.idEmp=i.idEmp AND v.idVariante=i.idVar AND v.estado='activo'
                    JOIN producto p ON p.idEmp=v.idEmp AND p.idProd=v.idProd AND p.estado='activo'
                    WHERE e.codigo='manaco'
                    ORDER BY i.cantDisp DESC, v.idVariante LIMIT 1
                """)).mappings().one()
                print(json.dumps(dict(row), default=str))
                return
        if args.mode == "cleanup":
            with db.begin() as conn:
                idemp = conn.execute(text(
                    "SELECT idEmp FROM empresa WHERE codigo='manaco'")).scalar_one()
                for operation in args.cleanup_uuid:
                    sale_rows = conn.execute(text("""
                        SELECT nroVenta AS "nroVenta", nroSuc AS "nroSuc", origen FROM venta
                        WHERE idEmp=:idemp AND idOperacion=:uuid
                    """), {"idemp": idemp, "uuid": operation}).all()
                    conn.execute(text("""
                        DELETE FROM bitacora
                        WHERE idEmp=:idemp AND detalles->>'idOperacion'=:uuid
                    """), {"idemp": idemp, "uuid": str(operation)})
                    conn.execute(text("""
                        DELETE FROM operacion_sync WHERE idEmp=:idemp AND idOperacion=:uuid
                    """), {"idemp": idemp, "uuid": operation})
                    for sale in sale_rows:
                        details = conn.execute(text("""
                            SELECT idVar AS "idVar", cantidad FROM detalleVenta
                            WHERE idEmp=:idemp AND nroVenta=:sale
                        """), {"idemp": idemp, "sale": sale.nroVenta}).all()
                        for detail in details:
                            conn.execute(text("""
                                UPDATE inventario SET stock=stock + :cantidad,
                                                       cantDisp=cantDisp + :cantidad
                                WHERE idEmp=:idemp AND nroSuc=:branch AND idVar=:variant
                            """), {"idemp": idemp, "branch": sale.nroSuc,
                                  "variant": detail.idVar, "cantidad": detail.cantidad})
                            conn.execute(text("""
                                DELETE FROM movimiento_inv m USING inventario i
                                WHERE m.idEmp=:idemp AND m.nroInv=i.nroInv
                                  AND i.idEmp=m.idEmp AND i.nroSuc=:branch AND i.idVar=:variant
                                  AND m.motivo=:motivo
                            """), {"idemp": idemp, "branch": sale.nroSuc,
                                  "variant": detail.idVar,
                                  "motivo": f"Venta {sale.origen} #{sale.nroVenta}"})
                        conn.execute(text(
                            "DELETE FROM pago WHERE idEmp=:idemp AND nroVenta=:sale"),
                            {"idemp": idemp, "sale": sale.nroVenta})
                        conn.execute(text(
                            "DELETE FROM venta WHERE idEmp=:idemp AND nroVenta=:sale"),
                            {"idemp": idemp, "sale": sale.nroVenta})
            return
        with db.connect() as conn:
            if args.uuid is None or not args.variant or not args.branch:
                parser.error("snapshot requiere --uuid, --variant y --branch")
            stock_before = args.stock_before
            row = conn.execute(text("""
                SELECT e.idEmp AS idemp, i.nroSuc AS branch, i.cantDisp AS stock
                FROM empresa e JOIN inventario i ON i.idEmp=e.idEmp
                WHERE e.codigo='manaco' AND i.nroSuc=:branch AND i.idVar=:variant
            """), {"branch": args.branch, "variant": args.variant}).mappings().one()
            idemp = row["idemp"]
            sale = conn.execute(text("""
                SELECT nroVenta AS "nroVenta", idOperacion::text AS operation, idVar
                FROM (
                  SELECT v.nroVenta,v.idOperacion,d.idVar FROM venta v
                  LEFT JOIN detalleVenta d ON d.idEmp=v.idEmp AND d.nroVenta=v.nroVenta
                  WHERE v.idEmp=:idemp AND v.idOperacion=:uuid
                ) x
            """), {"idemp": idemp, "uuid": args.uuid}).mappings().first()
            sync = conn.execute(text("""
                SELECT estado,codigoMotivo AS "codigoMotivo",nroVenta AS "nroVenta" FROM operacion_sync
                WHERE idEmp=:idemp AND idOperacion=:uuid
            """), {"idemp": idemp, "uuid": args.uuid}).mappings().first()
            if sale:
                nro = sale["nroVenta"]
                counts = conn.execute(text("""
                    SELECT (SELECT count(*) FROM venta WHERE idEmp=:idemp AND nroVenta=:sale) AS sales,
                           (SELECT count(*) FROM detalleVenta WHERE idEmp=:idemp AND nroVenta=:sale) AS details,
                           (SELECT count(*) FROM pago WHERE idEmp=:idemp AND nroVenta=:sale
                            AND metodo='efectivo' AND estado='aprobado') AS approved_cash,
                           (SELECT count(*) FROM movimiento_inv m JOIN inventario i
                            ON i.idEmp=m.idEmp AND i.nroInv=m.nroInv WHERE m.idEmp=:idemp
                            AND i.nroSuc=:branch AND i.idVar=:variant AND m.tipoMov='salida'
                            AND m.motivo='Venta pwa #'||CAST(:sale AS text)) AS stock_movements,
                           (SELECT count(*) FROM bitacora WHERE idEmp=:idemp
                            AND accion='venta_offline_confirmada'
                            AND detalles->>'idOperacion'=:uuid_text
                            AND (detalles->>'nroVenta')::integer=:sale) AS audit
                """), {"idemp": idemp, "sale": nro, "branch": row["branch"],
                      "variant": args.variant, "uuid_text": str(args.uuid)}).mappings().one()
            else:
                counts = {"sales": 0, "details": 0, "approved_cash": 0,
                          "stock_movements": 0, "audit": 0}
            output = {"operation": str(args.uuid), "sync_state": sync["estado"] if sync else None,
                      "sync_code": sync["codigoMotivo"] if sync else None,
                      "sale_no": sale["nroVenta"] if sale else None, "stock": row["stock"],
                      "stock_before": stock_before, **dict(counts)}
            print(json.dumps(output))
    finally:
        db.dispose()


if __name__ == "__main__":
    main()
