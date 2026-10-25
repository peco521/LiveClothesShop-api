from app.modules.seguridad_accesos.models import Bitacora


def add(db, *, action, user_id, ip, details):
    tenant = {"idemp": db.info["idemp"]} if db.info.get("multitenant") else {}
    db.add(Bitacora(usuario_id=user_id, accion=action, ip=ip, detalles=details, **tenant))
    db.flush()
