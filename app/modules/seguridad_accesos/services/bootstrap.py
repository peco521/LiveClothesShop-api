from uuid import uuid4

from sqlalchemy.exc import SQLAlchemyError

from app.core.security import Passwords
from app.modules.seguridad_accesos.models import Rol, Usuario
from app.modules.seguridad_accesos.repositories import admin, rol, usuario
from app.modules.seguridad_accesos.repositories.bootstrap import conflicting_role_identity, lock_writes
from app.modules.seguridad_accesos.repositories import bootstrap as bootstrap_repository
from app.modules.seguridad_accesos.schemas.bootstrap import SuperAdminInput


SUPERADMIN_ROL_ID = "superadmin"
INITIAL_PERMISSIONS = (
    ("CU05", "Gestionar Usuarios y Empleados"),
    ("CU06", "Gestionar Roles y Permisos"),
    ("CU07", "Gestionar Clientes"),
    ("CU08", "Consultar Bitácora"),
    ("CU09", "Gestionar Sucursales y Ciudades"),
)


def provision_initial_permissions(db, cliente_rol_id: str) -> tuple[int, int]:
    """Explicit catalog provisioning; never changes roles, users or profiles."""
    if cliente_rol_id != "cliente":
        raise BootstrapError("El catálogo inicial requiere CLIENTE_ROL_ID cliente.")
    try:
        with db.begin():
            bootstrap_repository.lock_permission_writes(db)
            for role_id, description in (("cliente", "Cliente"), (SUPERADMIN_ROL_ID, "SuperAdmin")):
                existing = rol.get(db, role_id)
                if existing is None:
                    raise BootstrapError("Falta un rol requerido; no se crearon roles.")
                if (existing.descripcion != description
                        or conflicting_role_identity(db, role_id, description)):
                    raise BootstrapError("Identidad o descripción de rol incompatible; operación cancelada.")
            if rol.public_role(db, cliente_rol_id) is None:
                raise BootstrapError("El rol Cliente tiene permisos o usuarios internos; operación cancelada.")
            functions_created = assignments_created = 0
            for permission_id, description in INITIAL_PERMISSIONS:
                created = bootstrap_repository.ensure_permission(
                    db, permission_id, description, SUPERADMIN_ROL_ID,
                    f"Permite {description}.",
                )
                if created is None:
                    raise BootstrapError("Función o relación existente contradictoria; transacción revertida.")
                functions_created += created[0]
                assignments_created += created[1]
            return functions_created, assignments_created
    except SQLAlchemyError:
        raise BootstrapError("No se pudo aprovisionar el catálogo; transacción revertida. "
                             "Revise conflictos, bloqueos o esquema.") from None


class BootstrapError(Exception):
    """Safe operator-facing error, containing no database/input details."""


def _ensure_role(db, role_id: str, description: str):
    if conflicting_role_identity(db, role_id, description):
        raise BootstrapError("La descripción del rol ya existe bajo otro ID; no se duplicó.")
    role = rol.get(db, role_id)
    if role is None:
        db.add(Rol(nro=role_id, descripcion=description))
        db.flush()
    elif role.descripcion != description:
        raise BootstrapError("El rol existente tiene una descripción incompatible; no se modificó.")


def provision(db, cliente_rol_id: str, data: SuperAdminInput | None = None,
              passwords: Passwords | None = None) -> bool:
    """Own one transaction. Return True only when a new admin was created."""
    if (not cliente_rol_id or cliente_rol_id != cliente_rol_id.strip()
            or len(cliente_rol_id) > 15 or cliente_rol_id.lower() == SUPERADMIN_ROL_ID):
        raise BootstrapError("CLIENTE_ROL_ID inválido o reservado para SuperAdmin.")
    if data is not None and passwords is None:
        raise BootstrapError("Se requiere el servicio de contraseñas para preparar SuperAdmin.")
    try:
        with db.begin():
            lock_writes(db)
            _ensure_role(db, cliente_rol_id, "Cliente")
            if rol.public_role(db, cliente_rol_id) is None:
                raise BootstrapError("El rol Cliente tiene permisos o usuarios internos; operación cancelada.")
            if data is None:
                return False
            _ensure_role(db, SUPERADMIN_ROL_ID, "SuperAdmin")
            matches = usuario.by_email(db, str(data.correo))
            if matches:
                if len(matches) != 1:
                    raise BootstrapError("El correo corresponde a varias cuentas; operación cancelada.")
                user = matches[0]
                if user.tipo != "A" or user.nrorol != SUPERADMIN_ROL_ID:
                    raise BootstrapError("El correo pertenece a un usuario incompatible; no se modificó.")
                profile = admin.get(db, user.idusuario)
                if profile is None:
                    raise BootstrapError("El usuario existente no tiene perfil admin; no se reparó.")
                if profile.cod_adm != data.cod_adm:
                    raise BootstrapError("cod_adm contradice el perfil admin existente; no se modificó.")
                if admin.has_incompatible_profile(db, user.idusuario):
                    raise BootstrapError("El usuario tiene un perfil Cliente o Empleado contradictorio.")
                return False
            user = Usuario(
                idusuario=str(uuid4()), ci=data.ci, nombre=data.nombre,
                apellidopat=data.apellidoPat, apellidomat=data.apellidoMat,
                sexo=data.sexo, correo=str(data.correo), telefono=data.telefono,
                direccion=data.direccion, fechanac=data.fechaNac,
                contrasena=passwords.hash(data.contrasena.get_secret_value()),
                tipo="A", nrorol=SUPERADMIN_ROL_ID,
            )
            usuario.add(db, user)
            admin.add(db, user.idusuario, data.cod_adm)
            return True
    except SQLAlchemyError:
        raise BootstrapError(
            "No se pudo completar el bootstrap: conflicto, bloqueo o esquema no disponible. "
            "La transacción fue revertida; revise el estado y reintente."
        ) from None
