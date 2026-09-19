from sqlalchemy import select

from app.modules.seguridad_accesos.models import Rol, RolFuncion, Usuario


def get(db, role_id: str):
    return db.get(Rol, role_id)


def permissions(db, role_id: str):
    return list(db.scalars(select(RolFuncion.idfun).where(RolFuncion.nrorol == role_id).order_by(RolFuncion.idfun)))


def public_role(db, role_id: str):
    role = db.scalar(select(Rol).where(Rol.nro == role_id).with_for_update())
    if role is None:
        return None
    # CU06: un rol dado de baja no puede representar el registro público.
    if role.estado != "activo":
        return None
    # CU01/CU02 require no business permissions. Fail closed if the configured
    # public role is assigned permissions or has been used by internal accounts.
    internal = db.scalar(select(Usuario.idusuario).where(
        Usuario.nrorol == role_id, Usuario.tipo != "C").limit(1))
    if internal or permissions(db, role_id):
        return None
    return role
