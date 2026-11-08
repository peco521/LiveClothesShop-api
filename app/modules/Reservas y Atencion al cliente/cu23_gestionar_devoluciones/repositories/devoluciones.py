"""Acceso a las operaciones transaccionales oficiales de CU23."""

from sqlalchemy import text


def registrar_con_procedimiento(
    db,
    *,
    idemp: int | None,
    nro_venta: int,
    monto,
    id_detalle_ventas: list[int],
    cantidades: list[int],
) -> int:
    """Crea devolución y detalles con el tenant explícito en el contrato SaaS."""
    if db.info.get("multitenant") is True:
        if idemp is None:
            raise ValueError("La sesión multitenant no contiene empresa")
        result = db.execute(text("""
            CALL sp_registrar_devolucion(
              :idemp, :nro_venta, :monto,
              CAST(:ids AS integer[]), CAST(:cantidades AS integer[]), NULL
            )
        """), {
            "idemp": idemp,
            "nro_venta": nro_venta,
            "monto": monto,
            "ids": id_detalle_ventas,
            "cantidades": cantidades,
        })
    else:
        result = db.execute(text("""
            CALL sp_registrar_devolucion(
              :nro_venta, :monto,
              CAST(:ids AS integer[]), CAST(:cantidades AS integer[]), NULL
            )
        """), {
            "nro_venta": nro_venta,
            "monto": monto,
            "ids": id_detalle_ventas,
            "cantidades": cantidades,
        })
    return int(result.scalar_one())


def aprobar_con_procedimiento(db, *, idemp: int | None, nro_dev: int) -> None:
    if db.info.get("multitenant") is True:
        if idemp is None:
            raise ValueError("La sesión multitenant no contiene empresa")
        db.execute(
            text("CALL sp_aprobar_devolucion(:idemp, :nro_dev)"),
            {"idemp": idemp, "nro_dev": nro_dev},
        )
    else:
        db.execute(
            text("CALL sp_aprobar_devolucion(:nro_dev)"),
            {"nro_dev": nro_dev},
        )


def aprobar_cambio_con_procedimiento(db, *, idemp: int, nro_cambio: int) -> None:
    db.execute(
        text("CALL sp_aprobar_cambio(:idemp, :nro_cambio)"),
        {"idemp": idemp, "nro_cambio": nro_cambio},
    )
