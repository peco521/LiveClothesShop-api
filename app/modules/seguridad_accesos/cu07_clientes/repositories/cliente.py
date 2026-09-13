from sqlalchemy import func, or_, select

from app.modules.seguridad_accesos.models import Admin, Cliente, Usuario
from app.modules.seguridad_accesos.cu05_usuarios_empleados.models import Empleado
from app.modules.seguridad_accesos.repositories import usuario


def list_candidates(db, *, offset, limit, q, activo):
    # Include malformed C users and non-C users carrying a client profile.
    # The service rejects an inconsistent page instead of silently hiding rows.
    has_client = select(Cliente.idusuario).where(Cliente.idusuario == Usuario.idusuario).exists()
    filters = [or_(Usuario.tipo == "C", has_client)]
    if activo is not None:
        filters.append(Usuario.activo == activo)
    if q:
        filters.append(or_(*(func.lower(field).contains(q.lower(), autoescape=True) for field in (
            Usuario.nombres, Usuario.apellidopat, Usuario.apellidomat, Usuario.correo, Usuario.ci))))
    total = db.scalar(select(func.count()).select_from(Usuario).where(*filters))
    rows = list(db.scalars(select(Usuario).where(*filters).order_by(
        Usuario.apellidopat, Usuario.nombres, Usuario.idusuario).offset(offset).limit(limit)))
    return rows, total


def get(db, user_id, *, lock=False):
    return usuario.locked(db, user_id) if lock else db.get(Usuario, user_id)


def profiles(db, user_id):
    return db.get(Admin, user_id), db.get(Empleado, user_id), db.get(Cliente, user_id)
