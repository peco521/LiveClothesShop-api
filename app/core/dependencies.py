import re

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.errors import DomainError
from app.modules.seguridad_accesos.services.auth import authentication_failed, current_session


def current_credential(request: Request):
    cookie = request.cookies.get(request.app.state.settings.cookie_name)
    authorization = request.headers.get("authorization")
    if authorization:
        scheme, _, credential = authorization.partition(" ")
        if scheme.lower() != "bearer" or cookie:
            raise authentication_failed()
    else:
        credential = cookie
    if not credential or not re.fullmatch(r"[A-Za-z0-9_-]{43}", credential):
        raise authentication_failed()
    return credential


def current_identity(credential: str = Depends(current_credential), db: Session = Depends(get_db)):
    return current_session(db, credential)


def require_permission(permission: str):
    def check(identity=Depends(current_identity)):
        if permission not in identity.permisos:
            raise DomainError(403, "acceso_denegado", "No tiene autorización para esta operación")
        return identity
    return check
