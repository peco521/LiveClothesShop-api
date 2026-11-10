from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.core.database import get_platform_db
from app.core.dependencies import current_credential
from app.core.errors import DomainError
from app.modules.seguridad_accesos.services import auth


def platform_identity(request: Request, credential: str = Depends(current_credential),
                      db: Session = Depends(get_platform_db)):
    """Sesión de plataforma verificada sin RLS de empresa.

    Rechaza tokens de empresa (el almacén de accesos marca `plataforma` al
    emitirlos) y exige que la cuenta siga siendo `tipo='S'` con idEmp NULL.
    """
    db.info["plataforma"] = True
    db.info["multitenant"] = request.app.state.settings.multitenant_enabled
    access = request.app.state.access_tokens.get(credential)
    if access is not None and not access.plataforma:
        auth.current_session(db, credential, request.app.state.access_tokens)
        raise DomainError(403, "acceso_denegado",
                          "Una sesión empresarial no autoriza operaciones de plataforma")
    _, identity = auth.platform_session(db, credential, request.app.state.access_tokens)
    # La carga de identidad lee usuario/roles y abre la transacción SQLAlchemy
    # de la solicitud. Finalizar esa lectura permite que una mutación posterior
    # abra su propia transacción atómica para procedimiento + auditoría.
    db.commit()
    return identity


def require_platform_permission(permission: str):
    def check(identity=Depends(platform_identity)):
        if permission not in identity.permisos:
            raise DomainError(403, "acceso_denegado",
                              "No tiene autorización para esta operación de plataforma")
        return identity
    return check


def current_platform_db(db: Session = Depends(get_platform_db)) -> Session:
    return db
