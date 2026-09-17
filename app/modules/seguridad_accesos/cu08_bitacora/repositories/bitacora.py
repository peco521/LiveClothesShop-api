from sqlalchemy import func, select

from app.modules.seguridad_accesos.models import Bitacora


def predicates(filters, dialect):
    conditions = []
    if filters.accion is not None:
        conditions.append(Bitacora.accion == filters.accion)
    if filters.usuario_id is not None:
        conditions.append(Bitacora.usuario_id == filters.usuario_id)
    for value, lower in ((filters.desde, True), (filters.hasta, False)):
        if value is not None:
            # Input is already UTC. SQLite's test adapter stores UTC without tzinfo.
            bound = value.replace(tzinfo=None) if dialect == "sqlite" else value
            conditions.append(Bitacora.fecha >= bound if lower else Bitacora.fecha <= bound)
    return conditions


def list_events(db, filters):
    conditions = predicates(filters, db.get_bind().dialect.name)
    total = db.scalar(select(func.count()).select_from(Bitacora).where(*conditions))
    # Include the displayed IP, but keep private details out of the list projection.
    rows = db.execute(select(Bitacora.id, Bitacora.usuario_id, Bitacora.accion, Bitacora.fecha, Bitacora.ip)
                      .where(*conditions).order_by(Bitacora.fecha.desc(), Bitacora.id.desc())
                      .offset(filters.offset).limit(filters.limit)).all()
    return rows, total


def get(db, event_id):
    return db.execute(select(Bitacora.id, Bitacora.usuario_id, Bitacora.accion,
                             Bitacora.fecha, Bitacora.ip, Bitacora.detalles)
                      .where(Bitacora.id == event_id)).one_or_none()
