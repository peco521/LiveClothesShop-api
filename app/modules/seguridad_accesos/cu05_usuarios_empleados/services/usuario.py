from contextlib import contextmanager
from uuid import uuid4

from sqlalchemy.exc import IntegrityError

from app.core.errors import DomainError
from app.core.security import utcnow
from app.modules.seguridad_accesos.models import Rol, Usuario
from app.modules.seguridad_accesos.repositories import usuario, sesion, recuperacion_contrasena
from app.modules.seguridad_accesos.services.bitacora import record
from app.modules.seguridad_accesos.cu05_usuarios_empleados.models import Empleado
from app.modules.seguridad_accesos.cu05_usuarios_empleados.repositories import usuario as repository
from app.modules.seguridad_accesos.cu05_usuarios_empleados.schemas.usuario import (
    AdminPerfil, CiudadOpcion, EmpleadoPerfil, SucursalOpcion, UsuarioDetalle, UsuariosListado,
)
from app.modules.seguridad_accesos.schemas.auth import RolResponse
from app.modules.seguridad_accesos.shared.services import continuidad

USER_FIELDS = {"ci": "ci", "nombres": "nombres", "apellidoPat": "apellidopat",
               "apellidoMat": "apellidomat", "sexo": "sexo", "correo": "correo",
               "telefono": "telefono", "direccion": "direccion", "fechaNac": "fechanac",
               "nroRol": "nrorol"}
EMPLOYEE_FIELDS = {"cod_emp": "cod_emp", "cargo": "cargo", "nroSuc": "nrosuc"}


@contextmanager
def transaction(db):
    # current_identity has already started this Session's transaction by reading.
    # Commit the SAME transaction, including all writes and audit events.
    try:
        yield
        db.commit()
    except IntegrityError:
        db.rollback()
        raise DomainError(409, "conflicto_integridad", "Los datos entran en conflicto con un registro existente") from None
    except Exception:
        db.rollback()
        raise


def coherent_profiles(db, user):
    admin, employee, client = repository.profiles(db, user.idusuario)
    valid = client is None and ((user.tipo == "E" and employee is not None and admin is None)
                              or (user.tipo == "A" and admin is not None and employee is None))
    if not valid:
        raise DomainError(409, "perfil_incoherente", "El usuario no tiene un perfil interno coherente")
    return admin, employee


def internal(db, user_id, *, lock=False):
    user = repository.internal(db, user_id, lock=lock)
    if user is None:
        raise DomainError(404, "usuario_no_encontrado", "Usuario interno no encontrado")
    coherent_profiles(db, user)
    return user


def detail(db, user):
    admin, employee = coherent_profiles(db, user)
    role = db.get(Rol, user.nrorol)
    if role is None:
        raise DomainError(409, "perfil_incoherente", "El usuario no tiene un rol válido")
    return UsuarioDetalle(
        idUsuario=user.idusuario, **{key: getattr(user, column) for key, column in USER_FIELDS.items()},
        tipo=user.tipo, activo=user.activo, rol=RolResponse(nro=role.nro, descripcion=role.descripcion),
        empleado=EmpleadoPerfil(cod_emp=employee.cod_emp, cargo=employee.cargo, nroSuc=employee.nrosuc) if employee else None,
        admin=AdminPerfil(cod_adm=admin.cod_adm) if admin else None,
    )


def list_users(db, **filters):
    users, total = repository.list_internal(db, **filters)
    return UsuariosListado(items=[detail(db, user) for user in users], total=total,
                           offset=filters["offset"], limit=filters["limit"])


def validate_role(db, role_id, settings):
    role = repository.role(db, role_id)
    if role is None:
        raise DomainError(422, "rol_no_encontrado", "El rol indicado no existe")
    # public_role() rejects any internal user sharing CLIENTE_ROL_ID: that would
    # disable CU01. This is not a SuperAdmin or permission-subset policy.
    if role.nro == settings.cliente_rol_id:
        raise DomainError(422, "rol_publico_no_asignable", "El rol de registro público no se puede asignar a usuarios internos")


def validate_branch(db, branch_id):
    if repository.branch(db, branch_id) is None:
        raise DomainError(422, "sucursal_no_encontrada", "La sucursal indicada no existe")


def validate_email(db, email, user_id=None):
    if any(match.idusuario != user_id for match in usuario.by_email(db, email)):
        raise DomainError(409, "correo_duplicado", "El correo ya está registrado")


def create_employee(db, data, settings, passwords, actor_id, peer):
    with transaction(db):
        validate_role(db, data.nroRol, settings)
        validate_branch(db, data.nroSuc)
        validate_email(db, str(data.correo))
        user = Usuario(idusuario=str(uuid4()), tipo="E", activo=True,
                       contrasena=passwords.hash(data.contrasena.get_secret_value()),
                       **{column: getattr(data, key) for key, column in USER_FIELDS.items()})
        usuario.add(db, user)
        repository.add_employee(db, Empleado(idusuario=user.idusuario, cod_emp=data.cod_emp,
                                            cargo=data.cargo, nrosuc=data.nroSuc))
        record(db, "usuario_creado", actor_id, peer, True)
        record(db, "empleado_creado", actor_id, peer, True)
        result = detail(db, user)
    return result


def edit_employee(db, user_id, data, settings, actor_id, peer):
    with transaction(db):
        before = continuidad.begin_change(db, actor_id, "CU05", settings.cliente_rol_id)
        user = internal(db, user_id, lock=True)
        if user.tipo != "E":
            raise DomainError(404, "empleado_no_encontrado", "Empleado no encontrado")
        _, employee = coherent_profiles(db, user)
        changes = data.model_dump(exclude_unset=True)
        # Validate resulting references, including legacy public-role assignments.
        validate_role(db, changes.get("nroRol", user.nrorol), settings)
        validate_branch(db, changes.get("nroSuc", employee.nrosuc))
        email = str(changes.get("correo", user.correo)).strip().lower()
        validate_email(db, email, user_id)
        if "correo" in changes:
            changes["correo"] = email
        changes = {key: value for key, value in changes.items()
                   if value != (getattr(user, USER_FIELDS[key]) if key in USER_FIELDS
                                else getattr(employee, EMPLOYEE_FIELDS[key]))}
        for key, column in USER_FIELDS.items():
            if key in changes:
                setattr(user, column, changes[key])
        if "correo" in changes:
            user.correo = email
        for key, column in EMPLOYEE_FIELDS.items():
            if key in changes:
                setattr(employee, column, changes[key])
        db.flush()
        continuidad.ensure_remaining(db, before, settings.cliente_rol_id)
        if any(key in USER_FIELDS for key in changes):
            record(db, "usuario_actualizado", actor_id, peer, True)
        if any(key in EMPLOYEE_FIELDS for key in changes):
            record(db, "empleado_actualizado", actor_id, peer, True)
        result = detail(db, user)
    return result


def set_state(db, user_id, data, actor_id, peer, settings):
    with transaction(db):
        before = continuidad.begin_change(db, actor_id, "CU05", settings.cliente_rol_id)
        user = internal(db, user_id, lock=True)
        if user.activo != data.activo:
            user.activo = data.activo
            continuidad.ensure_remaining(db, before, settings.cliente_rol_id)
            if not data.activo:
                now = utcnow()
                sesion.revoke_all(db, user_id, now)
                recuperacion_contrasena.invalidate(db, user_id, now)
            record(db, "usuario_activado" if data.activo else "usuario_desactivado", actor_id, peer, True)
        result = detail(db, user)
    return result


def roles(db, settings):
    return [RolResponse(nro=role.nro, descripcion=role.descripcion)
            for role in repository.roles(db, settings.cliente_rol_id)]


def cities(db):
    return [CiudadOpcion(id=city.id, nombre=city.nombre) for city in repository.cities(db)]


def branches(db, city_id):
    return [SucursalOpcion(nro=branch.nro, nombre=branch.nombre, direccion=branch.direccion,
                           estado=branch.estado, idCiud=city.id,
                           ciudad=CiudadOpcion(id=city.id, nombre=city.nombre))
            for branch, city in repository.branches(db, city_id)]
