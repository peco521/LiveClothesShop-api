from app.modules.seguridad_accesos.models import Cliente


def add(db, user_id: str, code: str):
    db.add(Cliente(idusuario=user_id, cod_cl=code))
    db.flush()
