"""Bounded E2E fixtures, only on the validated local test DB; never reset/seed."""
import argparse
import json
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, text
from serve_test_api import ROOT, read_test_urls


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "verify", "cleanup"))
    parser.add_argument("--manifest")
    args = parser.parse_args()
    _, platform_url, _, _ = read_test_urls()
    engine = create_engine(platform_url, hide_parameters=True)
    local = ROOT / ".local"
    try:
        if args.action == "prepare":
            demo = json.loads((local / "demo_credentials.json").read_text(encoding="utf-8"))
            marker = "E2ERC" + uuid4().hex[:20]
            rows = []
            with engine.begin() as conn:
                for code in ("manaco", "urbanwomen"):
                    original = next(e for e in demo["empresas"] if e["codigo"] == code)["usuarios"]["cliente"]
                    template = conn.execute(text("""SELECT p.idemp,p.idcat,p.idmarca,p.idcol,p.idprov,v.idtalla,
                        s.nro AS branch,s.nombre AS branch_name,c.nombre AS city FROM empresa e
                        JOIN producto p ON p.idemp=e.idemp JOIN varianteprod v ON v.idemp=p.idemp AND v.idprod=p.idprod
                        JOIN sucursal s ON s.idemp=e.idemp AND s.estado='activo'
                        JOIN ciudad c ON c.idemp=s.idemp AND c.id=s.idciud
                        WHERE e.codigo=:code AND p.estado='activo' AND v.estado='activo'
                        ORDER BY p.idprod,s.nro LIMIT 1"""), {"code": code}).mappings().one()
                    tenant = template["idemp"]
                    conn.execute(text("SELECT set_config('app.idemp', :tenant, true)"), {"tenant": str(tenant)})
                    user = marker + str(tenant)
                    email = user.lower() + "@example.com"
                    conn.execute(text("""INSERT INTO usuario(idusuario,ci,nombre,apellidopat,apellidomat,sexo,
                        correo,telefono,direccion,contrasena,fechanac,tipo,nrorol,idemp)
                        SELECT :user,:ci,'Reserva E2E','Prueba','Empresa',sexo,:email,telefono,direccion,
                            contrasena,fechanac,'C',nrorol,idemp FROM usuario
                        WHERE idemp=:tenant AND lower(btrim(correo))=:email_original"""),
                        {"user": user, "ci": uuid4().hex[:16], "email": email, "tenant": tenant,
                         "email_original": original["correo"].lower()})
                    conn.execute(text("INSERT INTO cliente(idusuario,cod_cl,idemp) VALUES (:user,:cod,:tenant)"),
                                 {"user": user, "cod": uuid4().hex[:10], "tenant": tenant})
                    product = marker + "-" + str(tenant)
                    variant = "R" + uuid4().hex[:14]
                    conn.execute(text("""INSERT INTO producto(idprod,descripcion,estado,esprueba,idpromo,idcat,idmarca,idcol,idprov,idemp)
                        VALUES (:product,:name,'activo',true,NULL,:cat,:brand,:col,:provider,:tenant)"""),
                        {"product": product, "name": f"Prenda reservas carrito E2E {code}", "cat": template["idcat"],
                         "brand": template["idmarca"], "col": template["idcol"], "provider": template["idprov"], "tenant": tenant})
                    conn.execute(text("""INSERT INTO varianteprod(idvariante,sku,precio,estado,idtalla,idprod,idemp)
                        VALUES (:variant,:variant,111,'activo',:talla,:product,:tenant)"""),
                        {"variant": variant, "talla": template["idtalla"], "product": product, "tenant": tenant})
                    conn.execute(text("LOCK TABLE inventario IN SHARE ROW EXCLUSIVE MODE"))
                    inventory = conn.scalar(text("SELECT coalesce(max(nroinv),0)+1 FROM inventario"))
                    conn.execute(text("""INSERT INTO inventario(nroinv,stock,cantdisp,nrosuc,idvar,idemp)
                        VALUES (:inv,7,7,:branch,:variant,:tenant)"""),
                        {"inv": inventory, "branch": template["branch"], "variant": variant, "tenant": tenant})
                    rows.append(dict(code=code, tenant=tenant, user=user, email=email,
                        password=original["contrasena"], product=product, variant=variant, inventory=inventory,
                        branch=template["branch"], branchName=template["branch_name"], city=template["city"]))
            path = local / f"reservas-carrito-e2e-{marker}.json"
            path.write_text(json.dumps({"marker": marker, "companies": rows}), encoding="utf-8")
            print(path)  # Path only; credentials remain in the ignored private manifest.
            return

        path = Path(args.manifest or "").resolve()
        if path.parent != local.resolve() or not path.name.startswith("reservas-carrito-e2e-E2ERC"):
            raise SystemExit("Solo se admite un manifiesto propio bajo .local")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not data["marker"].startswith("E2ERC"):
            raise SystemExit("Manifiesto no propio")
        if args.action == "verify":
            result = []
            with engine.connect() as conn:
                for row in data["companies"]:
                    result.append(dict(code=row["code"], stock=conn.scalar(text(
                        "SELECT cantdisp FROM inventario WHERE idemp=:tenant AND nroinv=:inv AND idvar=:variant"),
                        {"tenant": row["tenant"], "inv": row["inventory"], "variant": row["variant"]}),
                        reservations=conn.scalar(text("SELECT count(*) FROM reserva WHERE idemp=:tenant AND idusuariocl=:user"), row)))
            print(json.dumps(result))
            return
        with engine.begin() as conn:
            for row in data["companies"]:
                if not row["user"].startswith(data["marker"]) or not row["product"].startswith(data["marker"]):
                    raise SystemExit("Identificadores no propios")
                conn.execute(text("SELECT set_config('app.idemp', :tenant, true)"), {"tenant": str(row["tenant"])})
                for nro in conn.scalars(text("SELECT nroreserva FROM reserva WHERE idemp=:tenant "
                                            "AND idusuariocl=:user AND estado IN ('pendiente','confirmada')"), row).all():
                    conn.execute(text("CALL sp_cancelar_reserva(CAST(:tenant AS integer), CAST(:nro AS integer))"),
                                 {"tenant": row["tenant"], "nro": nro})
                for statement in (
                    "DELETE FROM operacion_sync WHERE idemp=:tenant AND idusuario=:user",
                    "DELETE FROM movimiento_inv WHERE idemp=:tenant AND nroinv=:inventory AND motivo IN (SELECT 'venta digital nro ' || nroventa::text FROM venta WHERE idemp=:tenant AND idusuariocl=:user)",
                    "DELETE FROM pago WHERE idemp=:tenant AND nroventa IN (SELECT nroventa FROM venta WHERE idemp=:tenant AND idusuariocl=:user)",
                    "DELETE FROM detalleventa WHERE idemp=:tenant AND nroventa IN (SELECT nroventa FROM venta WHERE idemp=:tenant AND idusuariocl=:user)",
                    "DELETE FROM venta WHERE idemp=:tenant AND idusuariocl=:user",
                    "DELETE FROM detallereserva WHERE idemp=:tenant AND nroreserva IN (SELECT nroreserva FROM reserva WHERE idemp=:tenant AND idusuariocl=:user)",
                    "DELETE FROM reserva WHERE idemp=:tenant AND idusuariocl=:user",
                    "DELETE FROM detallecarro WHERE idemp=:tenant AND idcarrito IN (SELECT idcarrito FROM carrito WHERE idemp=:tenant AND idusuariocl=:user)",
                    "DELETE FROM carrito WHERE idemp=:tenant AND idusuariocl=:user",
                    "DELETE FROM movimiento_inv WHERE idemp=:tenant AND nroinv IN (SELECT nroinv FROM inventario WHERE idemp=:tenant AND nroinv=:inventory AND idvar=:variant)",
                    "DELETE FROM inventario WHERE idemp=:tenant AND nroinv=:inventory AND idvar=:variant",
                    "DELETE FROM varianteprod WHERE idemp=:tenant AND idvariante=:variant AND idprod=:product",
                    "DELETE FROM producto WHERE idemp=:tenant AND idprod=:product",
                    "DELETE FROM promocion WHERE idemp=:tenant AND nombre=:promotion_name",
                    "DELETE FROM proveedor WHERE idemp=:tenant AND nombre=:supplier_name",
                    "DELETE FROM cliente WHERE idemp=:tenant AND idusuario=:user",
                    "DELETE FROM usuario WHERE idemp=:tenant AND idusuario=:user",
                ):
                    conn.execute(text(statement), {**row, 'supplier_name':row['product']+'-supplier', 'promotion_name':row['product']+'-promotion'})
                # Keep actual audit history. FK SET NULL preserves the events
                # when this run's disposable account is removed.
        path.unlink()
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
