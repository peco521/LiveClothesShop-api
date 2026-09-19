from contextlib import contextmanager
from uuid import uuid4
import secrets

from sqlalchemy.exc import IntegrityError

from app.core.errors import DomainError
from app.core.security import utcnow
from app.modules.seguridad_accesos.repositories import recuperacion_contrasena, rol, usuario
from app.modules.seguridad_accesos.services.bitacora import record
from app.modules.seguridad_accesos.shared.repositories import continuidad
from app.modules.seguridad_accesos.schemas.auth import RolResponse
from app.modules.seguridad_accesos.cu07_clientes.repositories import cliente as repository
from app.modules.seguridad_accesos.cu07_clientes.schemas.cliente import (
    ClienteDetalle, ClientePerfil, ClientesListado,
)

USER_FIELDS = {"ci": "ci", "nombre": "nombre", "apellidoPat": "apellidopat",
               "apellidoMat": "apellidomat", "sexo": "sexo", "correo": "correo",
               "telefono": "telefono", "direccion": "direccion", "fechaNac": "fechanac"}


@contextmanager
def transaction(db):
    # Authentication already began this session's transaction. Audit and recovery
    # invalidation must commit with the account update, never independently.
    try:
        yield
        db.commit()
    except IntegrityError:
        db.rollback()
        raise DomainError(409, "conflicto_integridad", "Los datos entran en conflicto con un registro existente") from None
    except Exception:
        db.rollback()
        raise


def incoherent():
    return DomainError(409, "perfil_incoherente", "El cliente no tiene un perfil o rol público coherente")


def profile(db, user):
    admin, employee, client = repository.profiles(db, user.idusuario)
    if user.tipo != "C" and client is None:
        raise DomainError(404, "cliente_no_encontrado", "Cliente no encontrado")
    if user.tipo != "C" or client is None or admin is not None or employee is not None:
        raise incoherent()
    return client


def public_role(db, settings):
    # Reuse CU01's locked role validation, including functions and internal users.
    # No CU06 catalog lock or isolation policy: valid clients have no functions
    # and cannot change CU06 eligibility. CU06 role writers lock this same role.
    role = rol.public_role(db, settings.cliente_rol_id)
    if role is None:
        raise incoherent()
    return role


def detail(db, user, role):
    client = profile(db, user)
    if user.nrorol != role.nro:
        raise incoherent()
    return ClienteDetalle(
        idUsuario=user.idusuario, **{key: getattr(user, column) for key, column in USER_FIELDS.items()},
        tipo="C", nroRol=user.nrorol,
        rol=RolResponse(nro=role.nro, descripcion=role.descripcion),
        cliente=ClientePerfil(cod_cl=client.cod_cl, estado=client.estado),
    )


def get(db, user_id, *, lock=False):
    user = repository.get(db, user_id, lock=lock)
    if user is None:
        raise DomainError(404, "cliente_no_encontrado", "Cliente no encontrado")
    profile(db, user)
    return user


def get_detail(db, user_id, settings):
    user = get(db, user_id)
    return detail(db, user, public_role(db, settings))


def list_clients(db, settings, **filters):
    users, total = repository.list_candidates(db, **filters)
    role = public_role(db, settings)
    return ClientesListado(items=[detail(db, user, role) for user in users], total=total,
                           offset=filters["offset"], limit=filters["limit"])


def revalidate_actor(db, actor_id):
    # Fresh identity/permission reads AFTER target/public-role lock waits.
    # These shared read helpers do not acquire the CU06 continuity lock.
    actor = continuidad.actor(db, actor_id)
    if actor is None:
        raise DomainError(401, "autenticacion_rechazada", "No se pudo autenticar la solicitud")
    if not continuidad.has_permission(db, actor.nrorol, "CU07"):
        raise DomainError(403, "acceso_denegado", "No tiene autorización para esta operación")


def create(db, data, settings, passwords, actor_id, peer):
    """CU07 alta administrativa: crea el cliente SIN cambiar la sesión del actor.

    Reutiliza la creación de CU01 (procedimiento almacenado en PostgreSQL) pero
    no emite cookie de sesión: la sesión del administrador permanece intacta.
    """
    email = str(data.correo)
    encoded = passwords.hash(data.contrasena.get_secret_value())
    user_id = str(uuid4())
    with transaction(db):
        role = public_role(db, settings)
        if any(match.idusuario != user_id for match in usuario.by_email(db, email)):
            raise DomainError(409, "correo_duplicado", "El correo ya está registrado")
        revalidate_actor(db, actor_id)
        usuario.create_client(db, user_id=user_id, data=data, password_hash=encoded,
                              role_id=role.nro, client_code=secrets.token_hex(5))
        record(db, "cliente_registrado", actor_id, peer, True)
        result = detail(db, get(db, user_id), role)
    return result


def set_state(db, user_id, data, settings, actor_id, peer):
    """CU07 baja lógica: desactiva o reactiva conservando ventas y reservas."""
    with transaction(db):
        user = get(db, user_id, lock=True)
        role = public_role(db, settings)
        detail(db, user, role)
        revalidate_actor(db, actor_id)
        client = profile(db, user)
        estado = "frecuente" if data.activo else "inactivo"
        if client.estado != estado:
            client.estado = estado
            db.flush()
            record(db, "cliente_activado" if data.activo else "cliente_desactivado",
                   actor_id, peer, True)
        result = detail(db, user, role)
    return result


def edit(db, user_id, data, settings, actor_id, peer):
    with transaction(db):
        user = get(db, user_id, lock=True)
        role = public_role(db, settings)
        detail(db, user, role)
        revalidate_actor(db, actor_id)
        changes = data.model_dump(exclude_unset=True)
        email = str(changes.get("correo", user.correo)).strip().lower()
        if any(match.idusuario != user_id for match in usuario.by_email(db, email)):
            raise DomainError(409, "correo_duplicado", "El correo ya está registrado")
        if "correo" in changes:
            changes["correo"] = email
        changes = {key: value for key, value in changes.items() if value != getattr(user, USER_FIELDS[key])}
        for key, value in changes.items():
            setattr(user, USER_FIELDS[key], value)
        if "correo" in changes:
            recuperacion_contrasena.invalidate(db, user_id, utcnow())
        if changes:
            record(db, "cliente_actualizado", actor_id, peer, True)
        result = detail(db, user, role)
    return result
