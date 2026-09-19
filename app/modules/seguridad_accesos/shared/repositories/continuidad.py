from sqlalchemy import func, or_, select

from app.core.errors import DomainError
from app.modules.seguridad_accesos.models import Funcion, Rol, RolFuncion, Usuario
from app.modules.seguridad_accesos.cu05_usuarios_empleados.models import Empleado


def lock(db):
    # All participating writers take this lock BEFORE user/role locks. PostgreSQL
    # READ COMMITTED gives subsequent queries a fresh snapshot after waiting.
    # Fail closed at other isolation levels rather than rely on a stale snapshot.
    # SQLite tests check policy/order only: SQLite does not implement FOR UPDATE.
    # External SQL/bootstrap writers must not run concurrently with this protocol.
    if db.get_bind().dialect.name == "postgresql":
        if db.connection().get_isolation_level() != "READ COMMITTED":
            raise DomainError(503, "aislamiento_no_compatible", "La operación requiere aislamiento READ COMMITTED")
    return db.scalar(select(Funcion).where(Funcion.id == "CU06").with_for_update()
                     .execution_options(populate_existing=True))


def actor(db, actor_id):
    return db.scalar(select(Usuario).where(Usuario.idusuario == actor_id)
                     .execution_options(populate_existing=True))


def has_permission(db, role_id, permission):
    # CU06: un rol dado de baja no autoriza ninguna función, ni siquiera a los
    # usuarios que ya lo tenían asignado.
    return db.scalar(select(RolFuncion.idfun).join(Rol, Rol.nro == RolFuncion.nrorol).where(
        RolFuncion.nrorol == role_id, RolFuncion.idfun == permission,
        Rol.estado == "activo")) is not None


def eligible_count(db, public_role_id):
    # CU05/CU06: un rol inactivo o un empleado dado de baja ya no cuenta como
    # usuario capaz de administrar CU06.
    return db.scalar(select(func.count()).select_from(Usuario).join(Rol, Rol.nro == Usuario.nrorol)
                     .join(RolFuncion, RolFuncion.nrorol == Rol.nro)
                     .outerjoin(Empleado, Empleado.idusuario == Usuario.idusuario).where(
                         Rol.nro != public_role_id, RolFuncion.idfun == "CU06",
                         Rol.estado == "activo",
                         or_(Usuario.tipo != "E", Empleado.estado == "activo")))
