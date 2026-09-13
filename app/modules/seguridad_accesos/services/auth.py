import secrets
from time import monotonic, sleep
from datetime import timedelta, timezone
from uuid import uuid4

from sqlalchemy.exc import IntegrityError

from app.core.errors import DomainError
from app.core.security import digest, new_credential, utcnow
from app.modules.seguridad_accesos.models import RecuperacionContrasena, Sesion, Usuario
from app.modules.seguridad_accesos.repositories import cliente, rol, sesion, usuario
from app.modules.seguridad_accesos.repositories import recuperacion_contrasena as recovery
from app.modules.seguridad_accesos.schemas.auth import AuthResponse, RegistroResponse, RolResponse, UsuarioResponse
from app.modules.seguridad_accesos.services.bitacora import record


def authentication_failed():
    return DomainError(401, "autenticacion_rechazada", "No se pudo autenticar la solicitud")


def register(db, data, settings, passwords, peer):
    email = str(data.correo)
    # Argon2 before opening the write transaction.
    encoded = passwords.hash(data.contrasena.get_secret_value())
    user_id = str(uuid4())
    try:
        with db.begin():
            if usuario.by_email(db, email):
                raise DomainError(409, "correo_duplicado", "El correo ya está registrado")
            if rol.public_role(db, settings.cliente_rol_id) is None:
                raise DomainError(503, "registro_no_disponible", "El registro no está disponible")
            user = Usuario(
                idusuario=user_id, ci=data.ci, nombres=data.nombres,
                apellidopat=data.apellidoPat, apellidomat=data.apellidoMat,
                sexo=data.sexo, correo=email, telefono=data.telefono,
                direccion=data.direccion, fechanac=data.fechaNac,
                contrasena=encoded, activo=True, tipo="C", nrorol=settings.cliente_rol_id,
            )
            usuario.add(db, user)
            # cod_cl is NOT unique in the official schema; identity is idusuario.
            cliente.add(db, user_id, secrets.token_hex(5))
            record(db, "cliente_registrado", user_id, peer, True)
    except IntegrityError:
        # The transaction has rolled back, including a partially-created profile.
        if usuario.by_email(db, email):
            raise DomainError(409, "correo_duplicado", "El correo ya está registrado") from None
        raise DomainError(503, "registro_no_disponible", "No se pudo completar el registro") from None
    return RegistroResponse(idUsuario=user_id, correo=email)


def public_identity(db, user, expires):
    role = rol.get(db, user.nrorol)
    if role is None:
        raise authentication_failed()
    return AuthResponse(
        usuario=UsuarioResponse(idUsuario=user.idusuario, nombres=user.nombres, correo=user.correo),
        rol=RolResponse(nro=role.nro, descripcion=role.descripcion),
        permisos=rol.permissions(db, role.nro), expiraEn=expires,
    )


def login(db, data, settings, passwords, peer):
    accepted = False
    credential = None
    response = None
    with db.begin():
        matches = usuario.by_email(db, str(data.correo), lock=True)
        user = matches[0] if len(matches) == 1 else None
        valid = passwords.verify(user.contrasena if user else None, data.contrasena.get_secret_value())
        if valid and user.activo and rol.get(db, user.nrorol) is not None:
            now = utcnow()
            expires = now + timedelta(hours=settings.session_hours)
            credential = new_credential()
            sesion.add(db, Sesion(usuario_id=user.idusuario, credencial_digest=digest(credential),
                                 creada_en=now, expira_en=expires))
            response = public_identity(db, user, expires)
            record(db, "login_correcto", user.idusuario, peer, True)
            accepted = True
        else:
            # No identity or email is recorded for rejected requests.
            record(db, "login_rechazado", None, peer, False)
    # Raise after committing the rejection event, not from inside the transaction.
    if not accepted:
        raise authentication_failed()
    return response, credential


def current_session(db, credential):
    _, identity = authenticated_session(db, credential)
    return identity


def authenticated_session(db, credential):
    session = sesion.by_digest(db, digest(credential))
    if session is None or session.revocada_en is not None:
        raise authentication_failed()
    expires = session.expira_en
    # SQLite test adapter does not preserve timezone info; PostgreSQL does.
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires <= utcnow():
        raise authentication_failed()
    user = db.get(Usuario, session.usuario_id)
    if user is None or not user.activo:
        raise authentication_failed()
    return session, public_identity(db, user, expires)


def logout(db, credential, peer):
    with db.begin():
        session, _ = authenticated_session(db, credential)
        if not sesion.revoke(db, session.id, utcnow()):
            raise authentication_failed()
        record(db, "logout_correcto", session.usuario_id, peer, True)


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
    user = matches[0] if len(matches) == 1 and matches[0].activo else None
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
        if user is None or not user.activo or not recovery.consume(db, token.id, now):
            raise rejected
        user.contrasena = encoded
        recovery.invalidate(db, user.idusuario, now)
        sesion.revoke_all(db, user.idusuario, now)
        record(db, "contrasena_restablecida", user.idusuario, peer, True)
    return {"mensaje": "Contraseña restablecida correctamente"}
