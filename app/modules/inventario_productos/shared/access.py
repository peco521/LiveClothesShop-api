import unicodedata
from contextlib import contextmanager

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.database import get_db
from app.core.dependencies import require_permission
from app.core.errors import DomainError
from app.modules.seguridad_accesos.models import Usuario
from app.modules.seguridad_accesos.cu05_usuarios_empleados.models.empleado import Empleado
from app.modules.seguridad_accesos.shared.repositories.continuidad import has_permission


def actor_scope(db, actor_id, permission, inventory=False):
    user = db.scalar(select(Usuario).where(Usuario.idusuario == actor_id)
                     .execution_options(populate_existing=True))
    if user is None:
        raise DomainError(401, "autenticacion_rechazada", "Inicia sesión nuevamente")
    if not has_permission(db, user.nrorol, permission):
        raise DomainError(403, "acceso_denegado", "No tienes permiso para esta operación")
    if user.tipo == "A":
        return None
    employee = db.scalar(select(Empleado).where(Empleado.idusuario == actor_id)
                         .execution_options(populate_existing=True)) if user.tipo == "E" else None
    cargo = ''.join(c for c in unicodedata.normalize('NFD', employee.cargo.lower())
                    if not unicodedata.combining(c)).strip() if employee else ''
    if inventory and employee and (cargo in {'encargado', 'encargada'} or cargo.startswith(('encargado ', 'encargada '))):
        return employee.nrosuc
    raise DomainError(403, "acceso_denegado", "Esta función requiere una cuenta autorizada")


def access(permission, inventory=False):
    def check(identity=Depends(require_permission(permission)), db=Depends(get_db)):
        scope = actor_scope(db, identity.usuario.idUsuario, permission, inventory)
        return identity.usuario.idUsuario, scope
    return check


@contextmanager
def transaction(db):
    try:
        yield
        db.commit()
    except IntegrityError:
        db.rollback()
        raise DomainError(409, "conflicto_integridad", "El registro está duplicado o relacionado con otros datos") from None
    except Exception:
        db.rollback()
        raise
