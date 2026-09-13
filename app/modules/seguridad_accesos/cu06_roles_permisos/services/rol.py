from contextlib import contextmanager

from sqlalchemy.exc import IntegrityError

from app.core.errors import DomainError
from app.modules.seguridad_accesos.models import Rol
from app.modules.seguridad_accesos.services.bitacora import record_role
from app.modules.seguridad_accesos.shared.services import continuidad
from app.modules.seguridad_accesos.cu06_roles_permisos.repositories import rol as repository
from app.modules.seguridad_accesos.cu06_roles_permisos.schemas.rol import (
    FuncionDetalle, PermisosDetalle, RolDetalle, RolesListado,
)


@contextmanager
def transaction(db):
    # Reuse the transaction started by current_identity, including the audit.
    try:
        yield
        db.commit()
    except IntegrityError:
        db.rollback()
        raise DomainError(409, "conflicto_integridad", "Los datos entran en conflicto con un registro existente") from None
    except Exception:
        db.rollback()
        raise


def get(db, role_id, *, lock=False):
    role = repository.get(db, role_id, lock=lock)
    if role is None:
        raise DomainError(404, "rol_no_encontrado", "Rol no encontrado")
    return role


def detail(role, settings):
    return RolDetalle(nro=role.nro, descripcion=role.descripcion, esRolCliente=role.nro == settings.cliente_rol_id)


def list_roles(db, settings, offset, limit):
    rows, total = repository.list_roles(db, offset, limit)
    return RolesListado(items=[detail(role, settings) for role in rows], total=total, offset=offset, limit=limit)


def functions(db):
    return [FuncionDetalle(id=row.id, descripcion=row.descripcion) for row in repository.functions(db)]


def permissions(db, role_id, settings):
    role = get(db, role_id)
    return PermisosDetalle(nroRol=role.nro, esRolCliente=role.nro == settings.cliente_rol_id,
                           permisos=[row.idfun for row in repository.assignments(db, role_id)])


def create(db, data, settings, actor_id, peer):
    with transaction(db):
        continuidad.begin_change(db, actor_id, "CU06", settings.cliente_rol_id)
        if repository.get(db, data.nro) is not None:
            raise DomainError(409, "rol_duplicado", "El identificador del rol ya existe")
        role = Rol(nro=data.nro, descripcion=data.descripcion)
        repository.add(db, role)
        record_role(db, "rol_creado", actor_id, peer, role.nro)
        result = detail(role, settings)
    return result


def edit(db, role_id, data, settings, actor_id, peer):
    with transaction(db):
        continuidad.begin_change(db, actor_id, "CU06", settings.cliente_rol_id)
        role = get(db, role_id, lock=True)
        if role.descripcion != data.descripcion:
            role.descripcion = data.descripcion
            record_role(db, "rol_actualizado", actor_id, peer, role.nro)
        result = detail(role, settings)
    return result


def replace_permissions(db, role_id, data, settings, actor_id, peer):
    with transaction(db):
        before = continuidad.begin_change(db, actor_id, "CU06", settings.cliente_rol_id)
        role = get(db, role_id, lock=True)
        desired = set(data.permisos)
        if role.nro == settings.cliente_rol_id and desired:
            raise DomainError(422, "rol_cliente_sin_funciones", "El rol cliente no puede recibir funciones")
        if repository.existing_functions(db, desired) != desired:
            raise DomainError(422, "funcion_no_encontrada", "Una o más funciones no existen")
        current = repository.assignments(db, role_id)
        old = {row.idfun for row in current}
        if desired != old:
            repository.replace(db, role_id, current, desired)
            continuidad.ensure_remaining(db, before, settings.cliente_rol_id)
            record_role(db, "permisos_rol_actualizados", actor_id, peer, role.nro,
                        agregadas=sorted(desired - old), retiradas=sorted(old - desired))
        result = PermisosDetalle(nroRol=role.nro, esRolCliente=role.nro == settings.cliente_rol_id,
                                permisos=sorted(desired))
    return result
