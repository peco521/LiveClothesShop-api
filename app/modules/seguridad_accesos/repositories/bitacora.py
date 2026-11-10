from app.modules.seguridad_accesos.models import Bitacora


def add(db, *, action, user_id, ip, details):
    # Platform identities have no tenant. Their audit rows retain idemp=NULL;
    # company requests must still provide the authenticated tenant explicitly.
    tenant = {"idemp": db.info["idemp"]} if db.info.get("multitenant") and not db.info.get("plataforma") else {}
    db.add(Bitacora(usuario_id=user_id, accion=action, ip=ip, detalles=details, **tenant))
    db.flush()
