from sqlalchemy import func, select, text

from app.core.database import is_postgresql
from app.modules.seguridad_accesos.models import Cliente, Usuario
from app.modules.seguridad_accesos.cu05_usuarios_empleados.models import Empleado
from app.modules.seguridad_accesos.repositories import cliente as cliente_repo


def by_email(db, email: str, *, lock=False):
    # Includes pre-existing addresses whose case was not normalized.
    query = select(Usuario).where(func.lower(func.trim(Usuario.correo)) == email).order_by(Usuario.idusuario)
    return list(db.scalars(query.with_for_update() if lock else query))


def locked(db, user_id):
    return db.scalar(select(Usuario).where(Usuario.idusuario == user_id).with_for_update().execution_options(populate_existing=True))


def add(db, usuario):
    db.add(usuario)
    db.flush()


def registrar_cliente(db, *, user_id, ci, nombre, apellido_pat, apellido_mat,
                      sexo, correo, telefono, direccion, password_hash,
                      fecha_nac, role_id, client_code):
    db.execute(text("""
        CALL sp_registrar_cliente(
          :user_id, :ci, :nombre, :apellido_pat, :apellido_mat, :sexo,
          :correo, :telefono, :direccion, :password_hash, :fecha_nac,
          :role_id, :client_code
        )
    """), {
        "user_id": user_id, "ci": ci, "nombre": nombre,
        "apellido_pat": apellido_pat, "apellido_mat": apellido_mat,
        "sexo": sexo, "correo": correo, "telefono": telefono,
        "direccion": direccion, "password_hash": password_hash,
        "fecha_nac": fecha_nac, "role_id": role_id, "client_code": client_code,
    })


def cambiar_contrasena(db, correo: str, password_hash: str):
    db.execute(text("CALL sp_cambiar_contrasena(:correo, :password_hash)"), {
        "correo": correo, "password_hash": password_hash,
    })


def create_client(db, *, user_id, data, password_hash, role_id, client_code):
    """Crea usuario cliente y su perfil a partir de datos ya validados.

    Reutilizado por CU01 (registro público) y CU07 (alta administrativa): la
    diferencia entre ambos flujos es la sesión, no la creación de la cuenta.
    """
    if is_postgresql(db):
        registrar_cliente(db, user_id=user_id, ci=data.ci, nombre=data.nombre,
                          apellido_pat=data.apellidoPat, apellido_mat=data.apellidoMat,
                          sexo=data.sexo, correo=str(data.correo), telefono=data.telefono,
                          direccion=data.direccion, password_hash=password_hash,
                          fecha_nac=data.fechaNac, role_id=role_id, client_code=client_code)
    else:
        # SQLite se conserva como sustituto de pruebas; la BD oficial
        # PostgreSQL usa el procedimiento almacenado anterior.
        user = Usuario(
            idusuario=user_id, ci=data.ci, nombre=data.nombre,
            apellidopat=data.apellidoPat, apellidomat=data.apellidoMat,
            sexo=data.sexo, correo=str(data.correo), telefono=data.telefono,
            direccion=data.direccion, fechanac=data.fechaNac,
            contrasena=password_hash, tipo="C", nrorol=role_id,
        )
        add(db, user)
        # cod_cl no es UNIQUE en el esquema oficial.
        cliente_repo.add(db, user_id, client_code)


def account_blocked(db, user) -> bool:
    """Baja lógica vigente: empleado inactivo (CU05) o cliente inactivo (CU07).

    Un usuario dado de baja no puede iniciar sesión ni seguir usando una sesión
    abierta; el historial y las relaciones se conservan intactos.
    """
    if user.tipo == "E":
        return db.scalar(select(Empleado.estado).where(
            Empleado.idusuario == user.idusuario)) == "inactivo"
    if user.tipo == "C":
        return db.scalar(select(Cliente.estado).where(
            Cliente.idusuario == user.idusuario)) == "inactivo"
    return False
