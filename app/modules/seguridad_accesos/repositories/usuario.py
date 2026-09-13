from sqlalchemy import func, select

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
