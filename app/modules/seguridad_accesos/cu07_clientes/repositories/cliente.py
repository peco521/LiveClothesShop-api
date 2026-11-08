from sqlalchemy import func, or_, select

from app.modules.seguridad_accesos.models import Admin, Cliente, Usuario
from app.modules.seguridad_accesos.cu05_usuarios_empleados.models import Empleado


def list_candidates(db, *, offset, limit, q):
    # Include malformed C users and non-C users carrying a client profile.
    # The service rejects an inconsistent page instead of silently hiding rows.
    has_client = select(Cliente.idusuario).where(Cliente.idusuario == Usuario.idusuario).exists()
    filters = [or_(Usuario.tipo == "C", has_client)]
    if db.info.get("multitenant") is True:
        filters.append(Usuario.idemp == (db.info.get("idemp") or 0))
    if q:
        filters.append(or_(*(func.lower(field).contains(q.lower(), autoescape=True) for field in (
            Usuario.nombre, Usuario.apellidopat, Usuario.apellidomat, Usuario.correo, Usuario.ci))))
    total = db.scalar(select(func.count()).select_from(Usuario).where(*filters))
    rows = list(db.scalars(select(Usuario).where(*filters).order_by(
        Usuario.apellidopat, Usuario.nombre, Usuario.idusuario).offset(offset).limit(limit)))
    return rows, total


def get(db, user_id, *, lock=False):
    query = select(Usuario).where(Usuario.idusuario == user_id)
    if db.info.get("multitenant") is True:
        query = query.where(Usuario.idemp == (db.info.get("idemp") or 0))
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    return db.scalar(query)


def profiles(db, user_id):
    return db.get(Admin, user_id), db.get(Empleado, user_id), db.get(Cliente, user_id)
