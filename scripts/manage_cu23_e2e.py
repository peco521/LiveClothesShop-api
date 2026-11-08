"""Own CU23 browser fixtures on the isolated local PostgreSQL test database."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, text

from serve_test_api import ROOT, read_test_urls


LOCAL = ROOT / ".local"


def _engine():
    _, platform_url = read_test_urls()
    return create_engine(platform_url, hide_parameters=True)


def _assert_target(connection):
    identity = connection.execute(text("""
        SELECT current_database(), current_user, inet_server_port(),
               (SELECT rolbypassrls FROM pg_roles WHERE rolname=current_user)
    """)).one()
    if identity[:3] != ("fashionstore_test", "fashionstore_provisioner", 55432):
        raise SystemExit("CU23 E2E solo permite el rol de plataforma en fashionstore_test local")
    if identity[3] is not True:
        raise SystemExit("El rol de plataforma local debe conservar su capacidad de administración")


def _prepare(connection):
    demo = json.loads((LOCAL / "demo_credentials.json").read_text(encoding="utf-8"))
    marker = "E2E23" + uuid4().hex[:10]
    original = next(row for row in demo["empresas"] if row["codigo"] == "manaco")["usuarios"]["cliente"]
    template = connection.execute(text("""
        SELECT e.idEmp, p.idCat, p.idMarca, p.idCol, p.idProv, v.idTalla,
               s.nro AS branch
        FROM empresa e
        JOIN producto p ON p.idEmp=e.idEmp
        JOIN varianteProd v ON v.idEmp=p.idEmp AND v.idProd=p.idProd
        JOIN sucursal s ON s.idEmp=e.idEmp AND s.estado='activo'
        WHERE e.codigo='manaco' AND p.estado='activo' AND v.estado='activo'
        ORDER BY p.idProd, s.nro
        LIMIT 1
    """)).mappings().one()
    tenant = template["idemp"]
    connection.execute(text("SELECT set_config('app.idemp', :tenant, true)"),
                       {"tenant": str(tenant)})
    user = marker + "C"
    email = user.lower() + "@example.com"
    product = marker + "P"
    variant = "R" + uuid4().hex[:14]
    connection.execute(text("""
        INSERT INTO usuario(idUsuario, ci, nombre, apellidoPat, apellidoMat, sexo,
                            correo, telefono, direccion, contrasena, fechaNac,
                            tipo, nroRol, idEmp)
        SELECT :user, :ci, 'Cliente E2E CU23', 'Prueba', 'FashionStore', sexo,
               :email, telefono, direccion, contrasena, fechaNac, 'C', nroRol, idEmp
        FROM usuario
        WHERE idEmp=:tenant AND lower(btrim(correo))=:original
    """), {
        "user": user, "ci": uuid4().hex[:16], "email": email, "tenant": tenant,
        "original": original["correo"].lower(),
    })
    connection.execute(text("""
        INSERT INTO cliente(idUsuario, cod_cl, idEmp)
        VALUES (:user, :code, :tenant)
    """), {"user": user, "code": uuid4().hex[:10], "tenant": tenant})
    connection.execute(text("""
        INSERT INTO producto(idProd, descripcion, estado, idPromo, idCat, idMarca,
                             idCol, idProv, idEmp)
        VALUES (:product, :name, 'activo', NULL, :category, :brand,
                :collection, :provider, :tenant)
    """), {
        "product": product, "name": f"Prenda {marker}", "category": template["idcat"],
        "brand": template["idmarca"], "collection": template["idcol"],
        "provider": template["idprov"], "tenant": tenant,
    })
    connection.execute(text("""
        INSERT INTO varianteProd(idVariante, sku, precio, estado, idTalla, idProd, idEmp)
        VALUES (:variant, :variant, 111, 'activo', :size, :product, :tenant)
    """), {
        "variant": variant, "size": template["idtalla"], "product": product, "tenant": tenant,
    })
    connection.execute(text("LOCK TABLE inventario IN SHARE ROW EXCLUSIVE MODE"))
    inventory = connection.scalar(text("SELECT coalesce(max(nroInv), 0) + 1 FROM inventario"))
    branch = template["branch"]
    connection.execute(text("""
        INSERT INTO inventario(nroInv, stock, cantDisp, nroSuc, idVar, idEmp)
        VALUES (:inventory, 1, 1, :branch, :variant, :tenant)
    """), {"inventory": inventory, "branch": branch, "variant": variant, "tenant": tenant})
    connection.execute(text("LOCK TABLE venta IN SHARE ROW EXCLUSIVE MODE"))
    sale = connection.scalar(text(
        "SELECT coalesce(max(nroVenta), 0) + 1 FROM venta WHERE idEmp=:tenant"),
        {"tenant": tenant},
    )
    connection.execute(text("""
        INSERT INTO venta(idEmp, nroVenta, total, desc_aplicado, estado,
                          idUsuarioCl, nroSuc)
        VALUES (:tenant, :sale, 111, 0, 'registrada', :user, :branch)
    """), {"tenant": tenant, "sale": sale, "user": user, "branch": branch})
    connection.execute(text("""
        INSERT INTO detalleVenta(idEmp, nroVenta, idDetalleVenta, cantidad,
                                 precioUnitario, descuentoUnitario, idVar,
                                 diasDevolucionAplicado, porcentajeDevolucionAplicado,
                                 condicionesDevolucionAplicadas)
        VALUES (:tenant, :sale, 1, 1, 111, 0, :variant, 30, 100, 'Revisión E2E')
    """), {"tenant": tenant, "sale": sale, "variant": variant})
    connection.execute(text("""
        UPDATE inventario SET stock=0, cantDisp=0
        WHERE idEmp=:tenant AND nroInv=:inventory AND idVar=:variant
    """), {"tenant": tenant, "inventory": inventory, "variant": variant})
    movement = connection.scalar(text("""
        INSERT INTO movimiento_inv(idEmp, tipoMov, cantidad, fecha, motivo, nroInv)
        VALUES (:tenant, 'salida', 1, CURRENT_DATE, :reason, :inventory)
        RETURNING idMov
    """), {
        "tenant": tenant, "reason": f"{marker} venta #{sale}", "inventory": inventory,
    })
    connection.execute(text("""
        INSERT INTO pago(idEmp, metodo, monto, estado, nroVenta)
        VALUES (:tenant, 'efectivo', 111, 'aprobado', :sale)
    """), {"tenant": tenant, "sale": sale})
    return {
        "marker": marker, "tenant": tenant, "user": user, "product": product,
        "variant": variant, "inventory": inventory, "branch": branch,
        "sale": sale, "movement": movement,
    }


def _manifest_path(argument: str | None = None) -> Path:
    path = Path(argument or "").resolve()
    if path.parent != LOCAL.resolve() or not path.name.startswith("cu23-e2e-E2E23"):
        raise SystemExit("Solo se admite un manifiesto CU23 propio bajo .local")
    return path


def _cleanup(connection, state):
    marker = state.get("marker", "")
    if not marker.startswith("E2E23") or not state.get("user", "").startswith(marker):
        raise SystemExit("Manifiesto CU23 no propio")
    tenant, sale, user = state["tenant"], state["sale"], state["user"]
    product, variant, inventory = state["product"], state["variant"], state["inventory"]
    if not product.startswith(marker) or len(variant) != 15:
        raise SystemExit("Las referencias CU23 no corresponden al manifiesto")
    connection.execute(text("SELECT set_config('app.idemp', :tenant, true)"),
                       {"tenant": str(tenant)})
    return_ids = connection.scalars(text("""
        SELECT nroDev FROM devolucion WHERE idEmp=:tenant AND nroVenta=:sale
    """), {"tenant": tenant, "sale": sale}).all()
    changes = connection.execute(text("""
        SELECT nroCambio AS "nroCambio", nroVentaNueva AS "nroVentaNueva"
        FROM cambio WHERE idEmp=:tenant AND nroVenta=:sale
    """), {"tenant": tenant, "sale": sale}).mappings().all()
    change_ids = [row["nroCambio"] for row in changes]
    sale_ids = [sale] + [row["nroVentaNueva"] for row in changes
                         if row["nroVentaNueva"] is not None]
    refund_ids = connection.scalars(text("""
        SELECT idReembolso FROM reembolso WHERE idEmp=:tenant AND nroDev=ANY(:returns)
    """), {"tenant": tenant, "returns": return_ids}).all() if return_ids else []
    payment_ids = connection.scalars(text("""
        SELECT idPago FROM pago WHERE idEmp=:tenant AND nroVenta=ANY(:sales)
    """), {"tenant": tenant, "sales": sale_ids}).all()
    connection.execute(text("""
        DELETE FROM bitacora WHERE idEmp=:tenant AND id>:audit_max AND (
          detalles->>'nroDev'=ANY(:returns) OR detalles->>'nroCambio'=ANY(:changes)
          OR detalles->>'idReembolso'=ANY(:refunds) OR detalles->>'idPago'=ANY(:payments))
    """), {
        "tenant": tenant, "audit_max": state["auditMax"],
        "returns": [str(value) for value in return_ids],
        "changes": [str(value) for value in change_ids],
        "refunds": [str(value) for value in refund_ids],
        "payments": [str(value) for value in payment_ids],
    })
    if return_ids:
        connection.execute(text(
            "DELETE FROM reembolso WHERE idEmp=:tenant AND nroDev=ANY(:returns)"),
            {"tenant": tenant, "returns": return_ids})
        connection.execute(text(
            "DELETE FROM detalleCambio WHERE idEmp=:tenant AND nroCambio=ANY(:changes)"),
            {"tenant": tenant, "changes": change_ids})
        connection.execute(text(
            "DELETE FROM cambio WHERE idEmp=:tenant AND nroCambio=ANY(:changes)"),
            {"tenant": tenant, "changes": change_ids})
        connection.execute(text(
            "DELETE FROM detalleDev WHERE idEmp=:tenant AND nroDev=ANY(:returns)"),
            {"tenant": tenant, "returns": return_ids})
        connection.execute(text(
            "DELETE FROM devolucion WHERE idEmp=:tenant AND nroDev=ANY(:returns)"),
            {"tenant": tenant, "returns": return_ids})
    connection.execute(text("DELETE FROM detalleVenta WHERE idEmp=:tenant AND nroVenta=ANY(:sales)"),
                       {"tenant": tenant, "sales": sale_ids})
    connection.execute(text("DELETE FROM pago WHERE idEmp=:tenant AND nroVenta=ANY(:sales)"),
                       {"tenant": tenant, "sales": sale_ids})
    connection.execute(text("DELETE FROM venta WHERE idEmp=:tenant AND nroVenta=ANY(:sales)"),
                       {"tenant": tenant, "sales": sale_ids})
    connection.execute(text("""
        DELETE FROM movimiento_inv WHERE idEmp=:tenant AND
          (nroInv=:inventory OR motivo=:reason)
    """), {"tenant": tenant, "inventory": inventory,
           "reason": f"{marker} venta #{sale}"})
    connection.execute(text("DELETE FROM inventario WHERE idEmp=:tenant AND nroInv=:inventory"),
                       {"tenant": tenant, "inventory": inventory})
    connection.execute(text("DELETE FROM variantecolors WHERE idEmp=:tenant AND idVar=:variant"),
                       {"tenant": tenant, "variant": variant})
    connection.execute(text("DELETE FROM politica_devolucion WHERE idEmp=:tenant AND idProd=:product"),
                       {"tenant": tenant, "product": product})
    connection.execute(text("DELETE FROM varianteProd WHERE idEmp=:tenant AND idVariante=:variant"),
                       {"tenant": tenant, "variant": variant})
    connection.execute(text("DELETE FROM producto WHERE idEmp=:tenant AND idProd=:product"),
                       {"tenant": tenant, "product": product})
    connection.execute(text("""
        DELETE FROM detallecarro WHERE idEmp=:tenant AND idCarrito IN
          (SELECT idCarrito FROM carrito WHERE idEmp=:tenant AND idUsuarioCl=:user)
    """), {"tenant": tenant, "user": user})
    connection.execute(text("DELETE FROM carrito WHERE idEmp=:tenant AND idUsuarioCl=:user"),
                       {"tenant": tenant, "user": user})
    connection.execute(text("DELETE FROM cliente WHERE idEmp=:tenant AND idUsuario=:user"),
                       {"tenant": tenant, "user": user})
    connection.execute(text("DELETE FROM usuario WHERE idEmp=:tenant AND idUsuario=:user"),
                       {"tenant": tenant, "user": user})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "verify", "cleanup"))
    parser.add_argument("--manifest")
    args = parser.parse_args()
    database = _engine()
    try:
        if args.action == "prepare":
            with database.begin() as connection:
                _assert_target(connection)
                state = _prepare(connection)
                state["auditMax"] = connection.scalar(text("SELECT coalesce(max(id), 0) FROM bitacora"))
            path = LOCAL / f"cu23-e2e-{state['marker']}.json"
            path.write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")
            print(path)
            return
        path = _manifest_path(args.manifest)
        state = json.loads(path.read_text(encoding="utf-8"))
        with database.begin() as connection:
            _assert_target(connection)
            if args.action == "verify":
                status = connection.scalar(text("""
                    SELECT estado FROM devolucion
                    WHERE idEmp=:tenant AND nroVenta=:sale
                """), {"tenant": state["tenant"], "sale": state["sale"]})
                stock = connection.execute(text("""
                    SELECT stock, cantDisp FROM inventario
                    WHERE idEmp=:tenant AND nroInv=:inventory
                """), {"tenant": state["tenant"], "inventory": state["inventory"]}).one()
                print(json.dumps({"returnState": status, "inventory": list(stock)}))
                return
            _cleanup(connection, state)
        path.unlink()
    finally:
        database.dispose()


if __name__ == "__main__":
    main()
