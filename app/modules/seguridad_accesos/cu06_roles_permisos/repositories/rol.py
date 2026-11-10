from sqlalchemy import func, select

from app.modules.seguridad_accesos.models import Funcion, Rol, RolFuncion, Usuario


def _tenant_filter(db, model):
    if db.info.get("multitenant") is True:
        return [model.idemp == (db.info.get("idemp") or 0)]
    return []


def get(db, role_id, *, lock=False):
    query = select(Rol).where(Rol.nro == role_id, *_tenant_filter(db, Rol)).execution_options(populate_existing=True)
    return db.scalar(query.with_for_update() if lock else query)


def list_roles(db, offset, limit):
    filters = _tenant_filter(db, Rol)
    total = db.scalar(select(func.count()).select_from(Rol).where(*filters))
    return list(db.scalars(select(Rol).where(*filters).order_by(Rol.nro).offset(offset).limit(limit))), total


def functions(db):
    return list(db.scalars(select(Funcion).where(~Funcion.id.startswith("SAAS-")).order_by(Funcion.id)))


def assignments(db, role_id):
    return list(db.scalars(select(RolFuncion).where(RolFuncion.nrorol == role_id)
                           .order_by(RolFuncion.idfun).execution_options(populate_existing=True)))


def existing_functions(db, ids):
    return set(db.scalars(select(Funcion.id).where(Funcion.id.in_(ids), ~Funcion.id.startswith("SAAS-"))))


def assigned_users(db, role_id):
    """Usuarios (internos o clientes) que hoy tienen asignado el rol."""
    return db.scalar(select(func.count()).select_from(Usuario).where(
        Usuario.nrorol == role_id, *_tenant_filter(db, Usuario)))


def add(db, role):
    db.add(role)
    db.flush()


def replace(db, role_id, current, desired):
    old = {assignment.idfun for assignment in current}
    for assignment in current:
        if assignment.idfun not in desired:
            db.delete(assignment)
    for function_id in sorted(desired - old):
        db.add(RolFuncion(nrorol=role_id, idfun=function_id,
                          descripcion=f"Permiso {function_id} asignado al rol {role_id}"))
    db.flush()
