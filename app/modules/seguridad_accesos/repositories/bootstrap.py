from sqlalchemy import func, select, text

from app.modules.seguridad_accesos.models import Funcion, Rol, RolFuncion


def conflicting_role_identity(db, role_id: str, description: str):
    return db.scalar(select(Rol.nro).where(
        func.lower(func.trim(Rol.descripcion)) == description.lower(),
        Rol.nro != role_id,
    ).limit(1)) is not None


def lock_writes(db):
    """Serialize provisioning, including absent rows, until commit/rollback."""
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        db.execute(text("SET LOCAL lock_timeout = '5s'"))
        # Also excludes concurrent non-bootstrap writers. No PG privileges changed.
        db.execute(text(
            "LOCK TABLE rol, usuario, admin, cliente, empleado "
            "IN SHARE ROW EXCLUSIVE MODE"
        ))
    elif dialect == "sqlite":
        # Isolated test adapter: SQLite has no SELECT FOR UPDATE/table locks.
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
    else:
        raise RuntimeError("Motor no compatible con el bootstrap")


def lock_permission_writes(db):
    """Protect role validation and absent permission rows until transaction end."""
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SET LOCAL lock_timeout = '5s'"))
        db.execute(text("LOCK TABLE rol, usuario, funcion, rol_funcion "
                        "IN SHARE ROW EXCLUSIVE MODE"))
    elif db.get_bind().dialect.name == "sqlite":
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
    else:
        raise RuntimeError("Motor no compatible con el bootstrap")


def ensure_permission(db, permission_id, description, role_id, assignment_description):
    """Insert missing rows only; return None on contradiction, else counts."""
    permission = db.get(Funcion, permission_id)
    assignment = db.get(RolFuncion, (role_id, permission_id))
    if ((permission is not None and permission.descripcion != description)
            or (assignment is not None and assignment.descripcion != assignment_description)):
        return None
    created = (int(permission is None), int(assignment is None))
    if permission is None:
        db.add(Funcion(id=permission_id, descripcion=description))
        db.flush()
    if assignment is None:
        db.add(RolFuncion(nrorol=role_id, idfun=permission_id,
                          descripcion=assignment_description))
        db.flush()
    return created
