from sqlalchemy import select, text, update
from sqlalchemy.exc import SQLAlchemyError

from app.core.database import is_postgresql
from app.modules.seguridad_accesos.models import RecuperacionContrasena


def by_digest(db, token_digest):
    return db.scalar(select(RecuperacionContrasena).where(RecuperacionContrasena.token_digest == token_digest))


def token_owner_by_digest(db, token_digest):
    """Devuelve (idemp, usuario_id) del token sin leer token_digest.

    Usa la función SECURITY DEFINER public.sp_recuperacion_token_por_digest,
    que ejecuta con privilegios del propietario y por tanto lee
    recuperacion_contrasena a pesar de RLS. Retorna solo idemp y usuario_id
    (nunca el resumen del token), por lo que no se filtra información de
    otros tenants. Retorna None si el token no existe o la función no está
    disponible (entorno sin la migración aplicada).
    """
    if not is_postgresql(db):
        return None
    try:
        fila = db.execute(
            text(
                "SELECT idemp, usuario_id FROM public.sp_recuperacion_token_por_digest(:d)"
            ),
            {"d": token_digest},
        ).first()
    except SQLAlchemyError:
        return None
    if fila is None:
        return None
    return int(fila[0]), fila[1]


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
    # Etiqueta la fila con el tenant de la sesión para que RLS la admita en
    # PostgreSQL (idemp = app.idemp). Sin esto la inserción se rechaza.
    if db.info.get("multitenant") and db.info.get("idemp") is not None:
        recovery.idemp = db.info["idemp"]
    db.add(recovery)
    db.flush()
