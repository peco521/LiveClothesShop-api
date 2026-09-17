from sqlalchemy import func, select, text

from app.modules.seguridad_accesos.models import Usuario


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
