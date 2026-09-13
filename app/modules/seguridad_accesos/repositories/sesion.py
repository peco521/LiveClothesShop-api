from sqlalchemy import select, update

from app.modules.seguridad_accesos.models import Sesion


def add(db, session):
    db.add(session)
    db.flush()


def by_digest(db, credential_digest: str):
    return db.scalar(select(Sesion).where(Sesion.credencial_digest == credential_digest))


def revoke(db, session_id: int, now):
    result = db.execute(update(Sesion).where(
        Sesion.id == session_id, Sesion.revocada_en.is_(None), Sesion.expira_en > now,
    ).values(revocada_en=now).execution_options(synchronize_session=False))
    return result.rowcount == 1


def revoke_all(db, user_id, now):
    db.execute(update(Sesion).where(Sesion.usuario_id == user_id, Sesion.revocada_en.is_(None)).values(revocada_en=now))
