from app.modules.seguridad_accesos.models import Bitacora


def add(db, *, action, user_id, ip, details):
    db.add(Bitacora(usuario_id=user_id, accion=action, ip=ip, detalles=details))
    db.flush()
