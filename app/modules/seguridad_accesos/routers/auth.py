from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import current_credential, current_identity
from app.modules.seguridad_accesos.schemas.auth import AuthResponse, Login, Registro, RegistroResponse
from app.modules.seguridad_accesos.services import auth
from app.modules.seguridad_accesos.schemas.auth import RecuperarContrasena, RestablecerContrasena
from app.modules.seguridad_accesos.services.recovery_delivery import get_recovery_delivery

router = APIRouter(prefix="/api/auth", tags=["Autenticación"])


def peer(request):
    return request.client.host if request.client else None


@router.post("/recuperar-contrasena", status_code=202)
def request_recovery(data: RecuperarContrasena, request: Request, db: Session = Depends(get_db)):
    return auth.request_recovery(db, data, get_recovery_delivery(request), peer(request))


@router.post("/restablecer-contrasena")
def reset_password(data: RestablecerContrasena, request: Request, db: Session = Depends(get_db)):
    return auth.reset_password(db, data, request.app.state.passwords, peer(request))


@router.post("/registro", response_model=RegistroResponse, status_code=201)
def register(data: Registro, request: Request, response: Response, db: Session = Depends(get_db)):
    # Registro público: crea la cuenta y deja la sesión iniciada (misma cookie que el login).
    result, credential = auth.register(db, data, request.app.state.settings,
                                       request.app.state.passwords, peer(request),
                                       request.app.state.access_tokens)
    _session_cookie(response, request.app.state.settings, credential)
    return result


@router.post("/login", response_model=AuthResponse)
def login(data: Login, request: Request, response: Response, db: Session = Depends(get_db)):
    # Compatibility endpoint for existing API consumers; web panels use the
    # type-restricted endpoints below. Permissions remain checked per operation.
    return _login(data, request, response, db)


@router.post("/login/cliente", response_model=AuthResponse)
def client_login(data: Login, request: Request, response: Response, db: Session = Depends(get_db)):
    return _login(data, request, response, db, allowed_types={"C"})


@router.post("/login/admin", response_model=AuthResponse)
def admin_login(data: Login, request: Request, response: Response, db: Session = Depends(get_db)):
    return _login(data, request, response, db, allowed_types={"A", "E"})


def _session_cookie(response: Response, settings, credential: str) -> None:
    # Misma sesión para login y registro: cookie HttpOnly, path /api y duración de sesión.
    response.set_cookie(settings.cookie_name, credential, httponly=True,
                        secure=settings.cookie_secure, samesite=settings.cookie_samesite,
                        path="/api", max_age=settings.session_hours * 3600)


def _login(data, request, response, db, allowed_types=None):
    settings = request.app.state.settings
    result, credential = auth.login(db, data, settings, request.app.state.passwords, peer(request),
                                    request.app.state.access_tokens, allowed_types=allowed_types)
    _session_cookie(response, settings, credential)
    return result


@router.get("/me", response_model=AuthResponse)
def me(identity=Depends(current_identity)):
    return identity


@router.post("/logout", status_code=204)
def logout(request: Request, credential: str = Depends(current_credential), db: Session = Depends(get_db)):
    auth.logout(db, credential, peer(request), request.app.state.access_tokens)
    settings = request.app.state.settings
    response = Response(status_code=204)
    response.delete_cookie(settings.cookie_name, path="/api", httponly=True,
                           secure=settings.cookie_secure, samesite=settings.cookie_samesite)
    return response
