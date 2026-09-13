from sqlalchemy import func, or_, select

from app.modules.seguridad_accesos.models import Admin, Cliente, Rol, Usuario
from app.modules.seguridad_accesos.cu05_usuarios_empleados.models import Empleado
from app.modules.seguridad_accesos.shared.repositories.organizacion import branch, branches, cities


def list_internal(db, *, offset, limit, q, tipo, activo):
    filters = [Usuario.tipo.in_(("A", "E"))]
    if tipo is not None:
        filters.append(Usuario.tipo == tipo)
    if activo is not None:
        filters.append(Usuario.activo == activo)
    if q:
        filters.append(or_(*(func.lower(field).contains(q.lower(), autoescape=True) for field in (
            Usuario.nombres, Usuario.apellidopat, Usuario.apellidomat, Usuario.correo, Usuario.ci))))
    total = db.scalar(select(func.count()).select_from(Usuario).where(*filters))
    rows = list(db.scalars(select(Usuario).where(*filters).order_by(
        Usuario.apellidopat, Usuario.nombres, Usuario.idusuario).offset(offset).limit(limit)))
    return rows, total


def internal(db, user_id, *, lock=False):
    query = select(Usuario).where(Usuario.idusuario == user_id, Usuario.tipo.in_(("A", "E")))
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    return db.scalar(query)


def profiles(db, user_id):
    return db.get(Admin, user_id), db.get(Empleado, user_id), db.get(Cliente, user_id)


def role(db, role_id):
    return db.scalar(select(Rol).where(Rol.nro == role_id).with_for_update())


def roles(db, public_role_id):
    return list(db.scalars(select(Rol).where(Rol.nro != public_role_id).order_by(Rol.descripcion, Rol.nro)))


def add_employee(db, employee):
    db.add(employee)
    db.flush()
