from sqlalchemy import select, update

from app.modules.seguridad_accesos.models import RecuperacionContrasena


def by_digest(db, token_digest):
    return db.scalar(select(RecuperacionContrasena).where(RecuperacionContrasena.token_digest == token_digest))


def invalidate(db, user_id, now):
    db.execute(update(RecuperacionContrasena).where(
        RecuperacionContrasena.usuario_id == user_id,
        RecuperacionContrasena.utilizada_en.is_(None),
    ).values(utilizada_en=now))


def consume(db, token_id, now):
    return db.execute(update(RecuperacionContrasena).where(
        RecuperacionContrasena.id == token_id,
        RecuperacionContrasena.utilizada_en.is_(None),
        RecuperacionContrasena.expira_en > now,
    ).values(utilizada_en=now).execution_options(synchronize_session=False)).rowcount == 1


def add(db, recovery):
    db.add(recovery)
    db.flush()
