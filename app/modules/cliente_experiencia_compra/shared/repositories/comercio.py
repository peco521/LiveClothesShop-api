"""Reservas e inventario compartidos (CU11; reutilizable por CU13/CU15)."""

from sqlalchemy import func, select

from app.modules.cliente_experiencia_compra.shared.models.catalogo import Inventario
from app.modules.cliente_experiencia_compra.shared.models.comercio import HorarioAtencion, HorarioSuc
from app.modules.seguridad_accesos.shared.models import Sucursal


def locked_inventario(db, idVar: str, nroSuc: int):
    """Fila de inventario bloqueada (SELECT ... FOR UPDATE en PostgreSQL)."""
    query = (select(Inventario)
             .where(Inventario.idvar == idVar, Inventario.nrosuc == nroSuc)
             .with_for_update()
             .execution_options(populate_existing=True))
    return db.scalar(query)


def horarios_sucursal(db, nroSuc: int):
    """Rangos [horaIni, horaFin] declarados para la sucursal (solo lectura)."""
    return list(db.execute(select(HorarioAtencion.horaini, HorarioAtencion.horafin, HorarioSuc.dias)
                           .join(HorarioSuc, HorarioSuc.idaten == HorarioAtencion.idaten)
                           .where(HorarioSuc.nrosuc == nroSuc)
                           .order_by(HorarioAtencion.horaini)).all())


def disponibilidad_global(db, variant_ids):
    """Suma de cantDisp por variante en sucursales activas (el carrito no tiene sucursal)."""
    if not variant_ids:
        return {}
    rows = db.execute(select(Inventario.idvar, func.coalesce(func.sum(Inventario.cantdisp), 0))
                      .join(Sucursal, (Sucursal.nro == Inventario.nrosuc) & (Sucursal.estado == "activo"))
                      .where(Inventario.idvar.in_(variant_ids))
                      .group_by(Inventario.idvar)).all()
    return {var_id: int(total or 0) for var_id, total in rows}


def locked_inventarios(db, idVar: str, nroSuc: int):
    """Todas las filas de inventario de la variante en la sucursal, bloqueadas y ordenadas."""
    query = (select(Inventario)
             .where(Inventario.idvar == idVar, Inventario.nrosuc == nroSuc)
             .order_by(Inventario.nroinv)
             .with_for_update()
             .execution_options(populate_existing=True))
    return list(db.scalars(query).all())
