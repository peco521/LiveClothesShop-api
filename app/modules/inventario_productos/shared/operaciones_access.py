import unicodedata
from fastapi import Depends
from sqlalchemy import select
from app.core.database import get_db
from app.core.dependencies import require_permission
from app.core.errors import DomainError
from app.modules.seguridad_accesos.models import Usuario
from app.modules.seguridad_accesos.cu05_usuarios_empleados.models.empleado import Empleado


def scope(db, identity, permission):
    user = db.get(Usuario, identity.usuario.idUsuario)
    if user and user.tipo == 'A':
        return user.idusuario, None
    employee = db.get(Empleado, user.idusuario) if user and user.tipo == 'E' else None
    cargo = ''.join(c for c in unicodedata.normalize('NFD', employee.cargo.lower())
                    if not unicodedata.combining(c)).strip() if employee else ''
    allowed = ('cajero', 'cajera') if permission == 'CU24' else ('encargado', 'encargada')
    if permission in {'CU22', 'CU23', 'CU24'} and employee and any(
            cargo == value or cargo.startswith(value + ' ') for value in allowed):
        return user.idusuario, employee.nrosuc
    raise DomainError(403, 'acceso_denegado', 'Tu cargo no está autorizado para esta operación')


def access(permission):
    def check(identity=Depends(require_permission(permission)), db=Depends(get_db)):
        return scope(db, identity, permission)
    return check


def branch(scope_id, actual):
    if scope_id is not None and actual != scope_id:
        raise DomainError(404, 'registro_no_encontrado', 'Registro no encontrado en tu sucursal')
