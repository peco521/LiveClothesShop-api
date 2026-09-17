import re

from sqlalchemy import func, or_, select, text

from app.core.database import is_postgresql
from app.core.errors import DomainError

from app.modules.seguridad_accesos.models import Admin, Cliente, Rol, Usuario
from app.modules.seguridad_accesos.cu05_usuarios_empleados.models import Empleado
from app.modules.seguridad_accesos.shared.repositories.organizacion import branch, branches, cities


def list_internal(db, *, offset, limit, q, tipo):
    filters = [Usuario.tipo.in_(("A", "E"))]
    if tipo is not None:
        filters.append(Usuario.tipo == tipo)
    if q:
        filters.append(or_(*(func.lower(field).contains(q.lower(), autoescape=True) for field in (
            Usuario.nombre, Usuario.apellidopat, Usuario.apellidomat, Usuario.correo, Usuario.ci))))
    total = db.scalar(select(func.count()).select_from(Usuario).where(*filters))
    rows = list(db.scalars(select(Usuario).where(*filters).order_by(
        Usuario.apellidopat, Usuario.nombre, Usuario.idusuario).offset(offset).limit(limit)))
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


def next_employee_code(db, *, lock=False):
    # Serialize allocation with insertion in the same PostgreSQL transaction.
    # Existing legacy codes are preserved; only Emp-<number> advances this counter.
    if lock and is_postgresql(db):
        db.execute(text("LOCK TABLE empleado IN SHARE ROW EXCLUSIVE MODE"))
    numbers = (int(match.group(1)) for code in db.scalars(select(Empleado.cod_emp))
               if (match := re.fullmatch(r"Emp-([0-9]{1,6})", code, re.IGNORECASE)))
    number = max(numbers, default=0) + 1
    if number > 999999:
        raise DomainError(409, "codigos_agotados", "No hay códigos de empleado disponibles")
    return f"Emp-{number:06d}"
