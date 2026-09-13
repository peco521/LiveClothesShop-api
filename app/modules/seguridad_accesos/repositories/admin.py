from sqlalchemy import column, select, table

from app.modules.seguridad_accesos.models import Admin, Cliente


def get(db, user_id: str):
    return db.get(Admin, user_id)


def add(db, user_id: str, cod_adm: str):
    db.add(Admin(idusuario=user_id, cod_adm=cod_adm))
    db.flush()


def has_incompatible_profile(db, user_id: str):
    # Read-only projection of the official table; no unrelated employee model.
    empleado = table("empleado", column("idusuario"))
    return (db.get(Cliente, user_id) is not None
            or db.scalar(select(empleado.c.idusuario).where(
                empleado.c.idusuario == user_id)) is not None)
