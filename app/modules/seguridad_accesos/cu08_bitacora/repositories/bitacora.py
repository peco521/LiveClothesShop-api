from sqlalchemy import case, func, select

from app.modules.seguridad_accesos.models import Bitacora, Usuario


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


def tenant_predicates(db):
    # Session context is established by require_permission -> current_session.
    # Fail closed if a tenant-aware caller has no validated company.
    if db.info.get("multitenant") is True:
        return [Bitacora.idemp == (db.info.get("idemp") or 0)]
    return []


def actor_projection(db):
    if db.info.get("multitenant") is True:
        own_actor = select(Usuario.idusuario).where(
            Usuario.idusuario == Bitacora.usuario_id,
            Usuario.idemp == (db.info.get("idemp") or 0)).correlate(Bitacora).exists()
        # Historical malformed references must not disclose another company's IDs.
        return case((own_actor, Bitacora.usuario_id), else_=None).label("usuario_id")
    return Bitacora.usuario_id


def list_events(db, filters):
    conditions = predicates(filters, db.get_bind().dialect.name)
    conditions.extend(tenant_predicates(db))
    actor = actor_projection(db)
    if filters.usuario_id is not None and db.info.get("multitenant") is True:
        conditions.append(actor == filters.usuario_id)
    total = db.scalar(select(func.count()).select_from(Bitacora).where(*conditions))
    # Include the displayed IP, but keep private details out of the list projection.
    rows = db.execute(select(Bitacora.id, actor, Bitacora.accion, Bitacora.fecha, Bitacora.ip)
                      .where(*conditions).order_by(Bitacora.fecha.desc(), Bitacora.id.desc())
                      .offset(filters.offset).limit(filters.limit)).all()
    return rows, total


def get(db, event_id):
    return db.execute(select(Bitacora.id, actor_projection(db), Bitacora.accion,
                             Bitacora.fecha, Bitacora.ip, Bitacora.detalles)
                      .where(Bitacora.id == event_id, *tenant_predicates(db))).one_or_none()
