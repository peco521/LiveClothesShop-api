from sqlalchemy import func, or_, select

from app.modules.seguridad_accesos.shared.models import Ciudad, Sucursal


def branch(db, branch_id, *, lock=True):
    query = select(Sucursal).where(Sucursal.nro == branch_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    return db.scalar(query)


def branches(db, city_id):
    query = select(Sucursal, Ciudad).join(Ciudad, Ciudad.id == Sucursal.idciud)
    if city_id is not None:
        query = query.where(Ciudad.id == city_id)
    return db.execute(query.order_by(Ciudad.nombre, Sucursal.nombre, Sucursal.nro)).all()


def cities(db):
    return list(db.scalars(select(Ciudad).order_by(Ciudad.nombre, Ciudad.id)))


def city(db, city_id, *, lock=False):
    query = select(Ciudad).where(Ciudad.id == city_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    return db.scalar(query)


def predicates(filters, *, branches=False):
    result = []
    if filters.q:
        fields = (Sucursal.nombre, Sucursal.direccion) if branches else (Ciudad.nombre,)
        result.append(or_(*(func.lower(field).contains(filters.q.lower(), autoescape=True) for field in fields)))
    if branches:
        if filters.idCiud is not None:
            result.append(Sucursal.idciud == filters.idCiud)
        if filters.estado is not None:
            result.append(Sucursal.estado == filters.estado)
    return result


def list_cities(db, filters):
    conditions = predicates(filters)
    total = db.scalar(select(func.count()).select_from(Ciudad).where(*conditions))
    rows = list(db.scalars(select(Ciudad).where(*conditions).order_by(Ciudad.nombre, Ciudad.id)
                           .offset(filters.offset).limit(filters.limit)))
    return rows, total


def list_branches(db, filters):
    conditions = predicates(filters, branches=True)
    query = select(Sucursal, Ciudad).join(Ciudad, Ciudad.id == Sucursal.idciud).where(*conditions)
    total = db.scalar(select(func.count()).select_from(Sucursal)
                      .join(Ciudad, Ciudad.id == Sucursal.idciud).where(*conditions))
    rows = db.execute(query.order_by(Ciudad.nombre, Sucursal.nombre, Sucursal.nro)
                      .offset(filters.offset).limit(filters.limit)).all()
    return rows, total


def add(db, row):
    db.add(row)
    db.flush()
