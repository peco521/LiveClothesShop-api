import secrets
from time import monotonic, sleep
from datetime import timedelta
from uuid import uuid4

from sqlalchemy.exc import IntegrityError

from app.core.database import is_postgresql
from app.core.errors import DomainError
from app.core.security import digest, new_credential, utcnow
from app.modules.seguridad_accesos.models import RecuperacionContrasena, Usuario
from app.modules.seguridad_accesos.repositories import rol, usuario
from app.modules.seguridad_accesos.repositories import recuperacion_contrasena as recovery
from app.modules.seguridad_accesos.schemas.auth import AuthResponse, RegistroResponse, RolResponse, UsuarioResponse
from app.modules.seguridad_accesos.services.bitacora import record


def authentication_failed():
    return DomainError(401, "autenticacion_rechazada", "No se pudo autenticar la solicitud")


def register(db, data, settings, passwords, peer, access_tokens):
    credential = None
    email = str(data.correo)
    # Prepare the configured password representation before the write transaction.
    encoded = passwords.hash(data.contrasena.get_secret_value())
    user_id = str(uuid4())
    try:
        with db.begin():
            if usuario.by_email(db, email):
                raise DomainError(409, "correo_duplicado", "El correo ya está registrado")
            if rol.public_role(db, settings.cliente_rol_id) is None:
                raise DomainError(503, "registro_no_disponible", "El registro no está disponible")
            client_code = secrets.token_hex(5)
            usuario.create_client(db, user_id=user_id, data=data, password_hash=encoded,
                                  role_id=settings.cliente_rol_id, client_code=client_code)
            record(db, "cliente_registrado", user_id, peer, True)
            # CU01: el registro público abre sesión con el mismo mecanismo que el
            # login (token en memoria + huella de la contraseña) y su misma auditoría.
            account = db.get(Usuario, user_id)
            if account is None:
                raise DomainError(503, "registro_no_disponible", "No se pudo completar el registro")
            expires = utcnow() + timedelta(hours=settings.session_hours)
            response = RegistroResponse(idUsuario=user_id, correo=email,
                                        sesion=public_identity(db, account, expires))
            credential = access_tokens.issue(account.idusuario, account.contrasena, expires)
    except IntegrityError:
        if credential is not None:
            access_tokens.revoke(credential)
        # The transaction has rolled back, including a partially-created profile.
        if usuario.by_email(db, email):
            raise DomainError(409, "correo_duplicado", "El correo ya está registrado") from None
        raise DomainError(503, "registro_no_disponible", "No se pudo completar el registro") from None
    except Exception:
        # Nunca dejar un acceso emitido si la operación no quedó confirmada.
        if credential is not None:
            access_tokens.revoke(credential)
        raise
    return response, credential


def public_identity(db, user, expires):
    role = rol.get(db, user.nrorol)
    # CU06: un rol dado de baja no autoriza ninguna función. CU05/CU07: una
    # cuenta dada de baja no inicia sesión ni continúa con una sesión abierta.
    if role is None or role.estado != "activo" or usuario.account_blocked(db, user):
        raise authentication_failed()
    return AuthResponse(
        usuario=UsuarioResponse(idUsuario=user.idusuario, tipo=user.tipo, nombre=user.nombre, correo=user.correo),
        rol=RolResponse(nro=role.nro, descripcion=role.descripcion),
        permisos=rol.permissions(db, role.nro), expiraEn=expires,
    )


def login(db, data, settings, passwords, peer, access_tokens, *, allowed_types=None):
    credential = None
    response = None
    try:
        with db.begin():
            matches = usuario.by_email(db, str(data.correo), lock=True)
            user = matches[0] if len(matches) == 1 else None
            password = data.contrasena.get_secret_value()
            valid, migrate = passwords.verify_for_login(user.contrasena if user else None, password)
            if (valid and user is not None and rol.get(db, user.nrorol) is not None
                    and (allowed_types is None or user.tipo in allowed_types)):
                if migrate:
                    encoded = passwords.hash(password)
                    if is_postgresql(db):
                        usuario.cambiar_contrasena(db, user.correo, encoded)
                        db.refresh(user, ["contrasena"])
                        if user.contrasena != encoded:
                            raise authentication_failed()
                    else:
                        user.contrasena = encoded
                    # Migration and login share the row lock and transaction.
                    # The access token must reference the NEW password digest.
                expires = utcnow() + timedelta(hours=settings.session_hours)
                response = public_identity(db, user, expires)
                credential = access_tokens.issue(user.idusuario, user.contrasena, expires)
                record(db, "login_correcto", user.idusuario, peer, True)
            else:
                # No identity or email is recorded for rejected requests.
                record(db, "login_rechazado", None, peer, False)
    except Exception:
        # La cookie se entrega solo al confirmar la auditoría. Si falla incluso
        # el commit, descartar también el acceso reservado en memoria.
        if credential is not None:
            access_tokens.revoke(credential)
        raise
    # Raise after committing the rejection event, not from inside the transaction.
    if credential is None:
        raise authentication_failed()
    return response, credential


def current_session(db, credential, access_tokens):
    _, identity = authenticated_session(db, credential, access_tokens)
    return identity


def authenticated_session(db, credential, access_tokens):
    access = access_tokens.get(credential)
    if access is None:
        raise authentication_failed()
    user = db.get(Usuario, access.usuario_id)
    # Cambiar la contraseña invalida también accesos previos y otros dispositivos.
    if user is None or not secrets.compare_digest(access.password_digest, digest(user.contrasena)):
        access_tokens.revoke(credential)
        raise authentication_failed()
    return access, public_identity(db, user, access.expira_en)


def logout(db, credential, peer, access_tokens):
    with db.begin():
        access, _ = authenticated_session(db, credential, access_tokens)
        record(db, "logout_correcto", access.usuario_id, peer, True)
    access_tokens.revoke(credential)


def request_recovery(db, data, delivery, peer):
    unavailable = DomainError(503, "recuperacion_no_disponible", "La recuperación de contraseña no está disponible")
    started = monotonic()
    token = new_credential()
    token_digest = digest(token)
    try:
        with db.begin():
            if delivery is None:
                record(db, "recuperacion_solicitada", None, peer, False)
            else:
                _issue_recovery(db, data, delivery, peer, token, token_digest)
    except Exception:
        # Neither adapter diagnostics nor SQL parameters may escape.
        raise unavailable from None
    finally:
        # Best-effort floor absorbs ordinary read/write differences, not slow I/O.
        # No expensive password hash is necessary for a recovery request.
        sleep(max(0, 0.25 - (monotonic() - started)))
    if delivery is None:
        raise unavailable
    return {"mensaje": "Si la cuenta puede recuperarse, recibirás instrucciones para restablecer tu contraseña."}


def _issue_recovery(db, data, delivery, peer, token, token_digest):
    matches = usuario.by_email(db, str(data.correo), lock=True)
    user = matches[0] if len(matches) == 1 else None
    now = utcnow()
    expires = now + timedelta(minutes=30)
    if user:
        recovery.invalidate(db, user.idusuario, now)
        recovery.add(db, RecuperacionContrasena(usuario_id=user.idusuario,
                     token_digest=token_digest, creada_en=now, expira_en=expires))
    record(db, "recuperacion_solicitada", None, peer, True)
    # Called for ALL addresses: adapter must implement equivalent dummy work.
    delivery.dispatch(user.correo if user else None, token, expires)


def reset_password(db, data, passwords, peer):
    rejected = DomainError(400, "recuperacion_invalida", "El enlace no es válido o ha expirado")
    encoded = passwords.hash(data.nueva_contrasena.get_secret_value())
    with db.begin():
        token = recovery.by_digest(db, digest(data.token.get_secret_value()))
        if token is None:
            raise rejected
        # Same lock order as issuance and login. Conditional consume rechecks after waiting.
        user = usuario.locked(db, token.usuario_id)
        now = utcnow()
        if user is None or not recovery.consume(db, token.id, now):
            raise rejected
        if is_postgresql(db):
            usuario.cambiar_contrasena(db, user.correo, encoded)
            db.expire(user, ["contrasena"])
        else:
            user.contrasena = encoded
        recovery.invalidate(db, user.idusuario, now)
        # authenticated_session compara la huella de la contraseña en cada petición.
        record(db, "contrasena_restablecida", user.idusuario, peer, True)
    return {"mensaje": "Contraseña restablecida correctamente"}
