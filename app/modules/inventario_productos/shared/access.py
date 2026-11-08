import unicodedata
from contextlib import contextmanager

from fastapi import Depends, Request
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.core.database import get_db
from app.core.dependencies import require_permission
from app.core.errors import DomainError
from app.modules.seguridad_accesos.models import Usuario
from app.modules.seguridad_accesos.cu05_usuarios_empleados.models.empleado import Empleado
from app.modules.seguridad_accesos.shared.repositories.continuidad import has_permission
from app.modules.seguridad_accesos.models import Rol, RolFuncion
from app.modules.inventario_productos.shared.tenant import select_for, tenant_id


def actor_scope(db, actor_id, permission, inventory=False):
    tenant = tenant_id(db)
    user = db.scalar(select_for(db, Usuario).where(Usuario.idusuario == actor_id)
                     .execution_options(populate_existing=True))
    if user is None:
        raise DomainError(401, "autenticacion_rechazada", "Inicia sesión nuevamente")
    permitted = (db.scalar(select_for(db, RolFuncion.idfun).join(Rol, Rol.nro == RolFuncion.nrorol).where(
        RolFuncion.nrorol == user.nrorol, RolFuncion.idfun == permission, Rol.estado == 'activo')) is not None
        if tenant is not None else has_permission(db, user.nrorol, permission))
    if not permitted:
        raise DomainError(403, "acceso_denegado", "No tienes permiso para esta operación")
    if tenant is not None and db.get_bind().dialect.name=='postgresql' and permission in {'CU18','CU19','CU20','CU21'}:
        if db.scalar(text("SELECT fn_tiene_funcionalidad(CAST(:tenant AS integer),CAST('PRODUCTOS' AS varchar))"),{'tenant':tenant}) is not True:
            raise DomainError(403,'funcionalidad_no_habilitada','El plan vigente no incluye gestión de productos')
    if user.tipo == "A":
        return None
    employee = db.scalar(select_for(db, Empleado).where(Empleado.idusuario == actor_id, Empleado.estado == 'activo')
                         .execution_options(populate_existing=True)) if user.tipo == "E" else None
    cargo = ''.join(c for c in unicodedata.normalize('NFD', employee.cargo.lower())
                    if not unicodedata.combining(c)).strip() if employee else ''
    if inventory and employee and (cargo in {'encargado', 'encargada'} or cargo.startswith(('encargado ', 'encargada '))):
        return employee.nrosuc
    raise DomainError(403, "acceso_denegado", "Esta función requiere una cuenta autorizada")


def access(permission, inventory=False):
    def check(request: Request, identity=Depends(require_permission(permission)), db=Depends(get_db)):
        if tenant_id(db) is not None:
            selectors = {'idemp', 'empresa', 'tenant', 'tenant_id', 'empresa_id'}
            if any(key.lower() in selectors for key in request.query_params) or any(
                    key in request.headers for key in ('x-tenant-id', 'x-empresa-id', 'x-empresa', 'x-idemp')):
                raise DomainError(422, 'empresa_desde_sesion', 'La empresa se obtiene de la sesión')
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
