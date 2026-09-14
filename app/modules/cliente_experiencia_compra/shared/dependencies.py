"""Dependencia de cliente autenticado (Decisión 1).

No usa RBAC ni nombre literal de rol. La autoridad es la identidad
autenticada + coherencia Usuario(tipo='C', activo) + perfil Cliente.
"""

from fastapi import Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import current_identity
from app.core.errors import DomainError
from app.modules.seguridad_accesos.models import Cliente, Usuario


def require_cliente(identity=Depends(current_identity), db: Session = Depends(get_db)):
    user = db.get(Usuario, identity.usuario.idUsuario)
    if user is None or not user.activo:
        raise DomainError(401, "autenticacion_rechazada", "No se pudo autenticar la solicitud")
    if user.tipo != "C":
        raise DomainError(403, "acceso_denegado", "No tiene autorización para esta operación")
    profile = db.get(Cliente, user.idusuario)
    if profile is None:
        raise DomainError(403, "acceso_denegado", "No tiene autorización para esta operación")
    return identity
