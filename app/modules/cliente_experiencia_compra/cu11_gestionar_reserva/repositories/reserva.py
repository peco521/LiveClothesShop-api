"""Consultas de reserva (CU11). El inventario se gestiona en shared comercio."""

from sqlalchemy import func, select, text

from app.modules.cliente_experiencia_compra.shared.models.catalogo import Producto, VarianteProd
from app.modules.cliente_experiencia_compra.shared.models.comercio import DetalleReserva, Reserva
from app.modules.cliente_experiencia_compra.shared.repositories.tenant import scoped


def _scoped(db, query):
    return scoped(db, query, Reserva, DetalleReserva, VarianteProd, Producto)


def locked_reserva(db, nroReserva: int):
    query = (select(Reserva).where(Reserva.nroreserva == nroReserva)
             .with_for_update().execution_options(populate_existing=True))
    return db.scalar(_scoped(db, query))


MARGEN_HORAS = 3


def detalles(db, nroReserva: int):
    return list(db.scalars(_scoped(db, select(DetalleReserva)
                           .where(DetalleReserva.nroreserva == nroReserva)
                           .order_by(DetalleReserva.iddetalleres))).all())


def variantes_productos(db, idVars):
    """Variantes con su producto para validación y descripción (una sola query)."""
    if not idVars:
        return {}
    rows = db.execute(_scoped(db, select(VarianteProd, Producto)
                      .join(Producto, Producto.idprod == VarianteProd.idprod)
                      .where(VarianteProd.idvariante.in_(idVars)))).all()
    return {variant.idvariante: (variant, product) for variant, product in rows}


def list_reservas(db, user_id: str, estado: str | None, offset: int, limit: int):
    conditions = [Reserva.idusuariocl == user_id]
    if estado is not None:
        conditions.append(Reserva.estado == estado)
    total = db.scalar(_scoped(db, select(func.count()).select_from(Reserva).where(*conditions))) or 0
    rows = list(db.scalars(_scoped(db, select(Reserva).where(*conditions)
                           .order_by(Reserva.fechareserva.desc(), Reserva.nroreserva.desc())
                           .offset(offset).limit(limit))).all())
    return rows, total


def vencidas(db, user_id: str, hoy):
    """Pendientes propias con fecha pasada (para liberación lazy)."""
    return list(db.scalars(_scoped(db, select(Reserva)
                           .where(Reserva.idusuariocl == user_id,
                                  Reserva.estado == "pendiente",
                                  Reserva.fechareserva < hoy)
                           .order_by(Reserva.nroreserva))).all())


def reservas_en_estado(db, user_id: str, estado: str):
    return list(db.scalars(_scoped(db, select(Reserva).where(
        Reserva.idusuariocl == user_id, Reserva.estado == estado
    ).order_by(Reserva.nroreserva))).all())


def add_reserva(db, row: Reserva):
    db.add(row)
    db.flush()
    return row


def add_detalle(db, row: DetalleReserva):
    db.add(row)
    db.flush()
    return row


def crear_con_procedimiento(db, *, user_id: str, nro_suc: int, fecha, hora,
                            id_vars: list[str], cantidades: list[int]) -> int:
    if db.info.get("multitenant") is True:
        return int(db.execute(text("""CALL sp_realizar_reserva(
            CAST(:tenant AS integer), CAST(:user AS varchar), CAST(:branch AS integer),
            CAST(:fecha AS date), CAST(:hora AS time), CAST(:variants AS varchar[]),
            CAST(:quantities AS integer[]), NULL)"""), {
                "tenant": db.info.get("idemp") or 0, "user": user_id, "branch": nro_suc,
                "fecha": fecha, "hora": hora, "variants": id_vars, "quantities": cantidades,
            }).scalar_one())
    result = db.execute(text("""
        CALL sp_realizar_reserva(
          :user_id, :nro_suc, :fecha, :hora,
          CAST(:id_vars AS varchar[]), CAST(:cantidades AS integer[]), NULL
        )
    """), {
        "user_id": user_id, "nro_suc": nro_suc, "fecha": fecha, "hora": hora,
        "id_vars": id_vars, "cantidades": cantidades,
    })
    return int(result.scalar_one())


def cancelar_con_procedimiento(db, nro_reserva: int):
    if db.info.get("multitenant") is True:
        db.execute(text("CALL sp_cancelar_reserva(CAST(:tenant AS integer), CAST(:nro AS integer))"),
                   {"tenant": db.info.get("idemp") or 0, "nro": nro_reserva})
        return
    db.execute(text("CALL sp_cancelar_reserva(:nro_reserva)"), {
        "nro_reserva": nro_reserva,
    })


def vencer_con_procedimiento(db, horas_margen: int = MARGEN_HORAS):
    db.execute(text("CALL sp_marcar_reservas_vencidas(:horas_margen)"), {
        "horas_margen": horas_margen,
    })
