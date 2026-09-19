"""Llamadas a las PA oficiales de CU23 (PostgreSQL).

La reposición de stock NO se realiza aquí ni en el servicio: al pasar la
devolución a 'aprobada' el trigger `trg_reponer_stock_devolucion` es el único
responsable de reponer stock/cantDisp y registrar el movimiento.
"""
from sqlalchemy import text


def registrar_con_procedimiento(db, *, nro_venta: int, monto, id_detalle_ventas: list[int],
                                cantidades: list[int]) -> int:
    """CALL sp_registrar_devolucion: crea la devolución y su detalle."""
    result = db.execute(text("""
        CALL sp_registrar_devolucion(
          :nro_venta, :monto,
          CAST(:ids AS integer[]), CAST(:cantidades AS integer[]), NULL
        )
    """), {
        "nro_venta": nro_venta, "monto": monto,
        "ids": id_detalle_ventas, "cantidades": cantidades,
    })
    return int(result.scalar_one())


def aprobar_con_procedimiento(db, nro_dev: int):
    """CALL sp_aprobar_devolucion: el UPDATE dispara la reposición por trigger."""
    db.execute(text("CALL sp_aprobar_devolucion(:nro_dev)"), {"nro_dev": nro_dev})