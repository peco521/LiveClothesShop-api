from app.core.errors import DomainError
from app.modules.seguridad_accesos.shared.repositories import continuidad as repository


def begin_change(db, actor_id, permission, public_role_id):
    repository.lock(db)
    # The request dependency ran before the lock, possibly before another commit.
    actor = repository.actor(db, actor_id)
    if actor is None or not actor.activo:
        raise DomainError(401, "autenticacion_rechazada", "No se pudo autenticar la solicitud")
    if not repository.has_permission(db, actor.nrorol, permission):
        raise DomainError(403, "acceso_denegado", "No tiene autorización para esta operación")
    return repository.eligible_count(db, public_role_id)


def ensure_remaining(db, before, public_role_id):
    # Initial zero must not block unrelated changes or recovery. Only transitions
    # from some eligible users to none are forbidden; no role-name bypass.
    db.flush()
    if before > 0 and repository.eligible_count(db, public_role_id) == 0:
        raise DomainError(409, "ultimo_usuario_cu06", "Debe conservar al menos un usuario activo con permiso CU06")
