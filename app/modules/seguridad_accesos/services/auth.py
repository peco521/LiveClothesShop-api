import secrets
from time import monotonic, sleep
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.core.database import is_postgresql, tenant_context
from app.core.errors import DomainError
from app.core.security import digest, new_credential, utcnow
from app.modules.seguridad_accesos.models import RecuperacionContrasena, Usuario
from app.modules.seguridad_accesos.repositories import rol, usuario
from app.modules.seguridad_accesos.repositories import recuperacion_contrasena as recovery
from app.modules.seguridad_accesos.schemas.auth import AuthResponse, RegistroResponse, RolResponse, UsuarioResponse
from app.modules.seguridad_accesos.services.bitacora import record


def authentication_failed():
    return DomainError(401, "autenticacion_rechazada", "No se pudo autenticar la solicitud")


def resolver_idemp_empresa(db, settings, codigo_empresa):
    """Resuelve el código de empresa a idEmp (tenant) sin aceptar idEmp del cliente.

    Solo aplica en modo multitenant. La empresa debe estar activa con una suscripción
    vigente ("empresa elegible"); auth_resolver_empresa devuelve NULL en caso
    contrario. En PostgreSQL se valida mediante la función restringida SECURITY
    DEFINER; el cliente nunca envía ni el servidor acepta un idEmp arbitrario.
    """
    if not settings.multitenant_enabled:
        return None
    codigo = str(codigo_empresa or "").strip().lower()
    if not codigo:
        raise DomainError(422, "empresa_requerida",
                          "Se requiere el código de empresa para el registro público")
    if is_postgresql(db):
        idemp = db.scalar(text("SELECT auth_resolver_empresa(:codigo)"),
                          {"codigo": codigo})
        if idemp is None:
            raise DomainError(403, "empresa_no_elegible",
                              "La empresa no está disponible para el registro público")
        return int(idemp)
    # Sin PostgreSQL no existe la función de resolución: el registro público
    # multitenant requiere la base oficial.
    raise DomainError(503, "registro_no_disponible",
                      "El registro público multitenant requiere PostgreSQL")


def public_client_role_tenant(db, idemp: int):
    """Rol cliente del tenant (E{idemp}-CLI): activo y vinculado al tenant resuelto.

    Coherente con el rol que ya usa el login de cliente
    (cliente_rol_id = E{idemp}-CLI). El registro público asigna este rol desde el
    servidor; el cliente nunca elige rol, tipo ni tenant desde el payload.
    """
    role = rol.get(db, f"E{idemp}-CLI")
    if role is None or role.idemp != idemp or role.estado != "activo":
        return None
    return role


def register(db, data, settings, passwords, peer, access_tokens):
    credential = None
    email = str(data.correo)
    encoded = passwords.hash(data.contrasena.get_secret_value())
    user_id = str(uuid4())
    try:
        with db.begin():
            idemp = resolver_idemp_empresa(db, settings, getattr(data, "empresa", None))
            if db.info.get("multitenant"):
                tenant_context(db, idemp)
                db.info["empresa"] = str(data.empresa)
                role = public_client_role_tenant(db, idemp)
            else:
                role = rol.public_role(db, settings.cliente_rol_id)
            if role is None:
                raise DomainError(503, "registro_no_disponible", "El registro no está disponible")
            # Unicidad de correo por empresa (tenant de sesión), nunca global.
            if usuario.by_email(db, email):
                raise DomainError(409, "correo_duplicado", "El correo ya está registrado")
            client_code = secrets.token_hex(5)
            usuario.create_client(db, user_id=user_id, data=data, password_hash=encoded,
                                  role_id=role.nro, client_code=client_code, idemp=idemp)
            record(db, "cliente_registrado", user_id, peer, True)
            # CU01: el registro público abre sesión con el mismo mecanismo que el
            # login (token en memoria + huella de la contraseña) y su misma auditoría.
            account = db.get(Usuario, user_id)
            if account is None:
                raise DomainError(503, "registro_no_disponible", "No se pudo completar el registro")
            expires = utcnow() + timedelta(hours=settings.session_hours)
            response = RegistroResponse(idUsuario=user_id, correo=email,
                                        sesion=public_identity(db, account, expires))
            credential = access_tokens.issue(account.idusuario, account.contrasena, expires,
                                             db.info.get("idemp"))
    except IntegrityError:
        if credential is not None:
            access_tokens.revoke(credential)
        if usuario.by_email(db, email):
            raise DomainError(409, "correo_duplicado", "El correo ya está registrado") from None
        raise DomainError(503, "registro_no_disponible", "No se pudo completar el registro") from None
    except Exception:
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
    if db.info.get("multitenant") and (user.idemp != db.info.get("idemp") or role.idemp != user.idemp):
        raise authentication_failed()
    if user.tipo == "S":
        # Administrador de plataforma: idEmp NULL, rol SAAS-ADMIN y permisos
        # propios (SAAS-*), nunca permisos empresariales CU05..CU25.
        if user.idemp is not None or role.idemp is not None:
            raise authentication_failed()
        return AuthResponse(
            empresa=None, plataforma=True,
            usuario=UsuarioResponse(idUsuario=user.idusuario, tipo=user.tipo, nombre=user.nombre, correo=user.correo),
            rol=RolResponse(nro=role.nro, descripcion=role.descripcion),
            permisos=[p for p in rol.permissions(db, role.nro) if p.startswith("SAAS-")],
            expiraEn=expires,
        )
    if db.info.get("multitenant") and (user.idemp != db.info.get("idemp") or role.idemp != user.idemp):
        raise authentication_failed()
    company = db.info.get("empresa")
    if db.info.get("multitenant") and not company:
        raise authentication_failed()
    return AuthResponse(
        empresa=company,
        usuario=UsuarioResponse(idUsuario=user.idusuario, tipo=user.tipo, nombre=user.nombre, correo=user.correo),
        rol=RolResponse(nro=role.nro, descripcion=role.descripcion),
        permisos=rol.permissions(db, role.nro), expiraEn=expires,
    )


def login(db, data, settings, passwords, peer, access_tokens, *, allowed_types=None):
    credential = None
    response = None
    if allowed_types is None:
        # El login de empresa nunca admite al administrador de plataforma.
        allowed_types = {"A", "C", "E"}
    try:
        with db.begin():
            if settings.multitenant_enabled:
                if not data.empresa or not is_postgresql(db):
                    raise authentication_failed()
                # The only pre-tenant lookup is a restricted SECURITY DEFINER function.
                idemp = db.scalar(text("SELECT auth_resolver_empresa(:codigo)"), {"codigo": data.empresa})
                if idemp is None:
                    raise authentication_failed()
                tenant_context(db, idemp)
                db.info["empresa"] = data.empresa
            matches = usuario.by_email(db, str(data.correo), lock=True)
            user = matches[0] if len(matches) == 1 else None
            password = data.contrasena.get_secret_value()
            valid, migrate = passwords.verify_for_login(user.contrasena if user else None, password)
            if user is not None and user.tipo == "S":
                # Platform accounts never accept or migrate to plaintext passwords.
                valid = valid and passwords.storage == "argon2id" and user.contrasena.startswith("$argon2id$")
                migrate = False
            if (valid and user is not None and (user.tipo == "S" or rol.get(db, user.nrorol) is not None)
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
                credential = access_tokens.issue(user.idusuario, user.contrasena, expires, db.info.get("idemp"))
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


def login_platform(db, data, settings, passwords, peer, access_tokens):
    """Login del administrador SaaS sobre la conexión de plataforma.

    `db` debe provenir del motor de aprovisionamiento (BYPASSRLS): la cuenta de
    plataforma tiene idEmp NULL y no es visible para el rol HTTP de empresa.
    """
    credential = None
    response = None
    try:
        with db.begin():
            matches = usuario.by_email(db, str(data.correo), lock=True)
            user = matches[0] if len(matches) == 1 else None
            password = data.contrasena.get_secret_value()
            valid, _ = passwords.verify_for_login(user.contrasena if user else None, password)
            if (valid and user is not None and user.tipo == "S" and user.idemp is None
                    and rol.get(db, user.nrorol) is not None):
                expires = utcnow() + timedelta(hours=settings.session_hours)
                response = public_identity(db, user, expires)
                credential = access_tokens.issue(user.idusuario, user.contrasena, expires,
                                                 None, plataforma=True)
                record(db, "login_correcto", user.idusuario, peer, True)
            else:
                record(db, "login_rechazado", None, peer, False)
    except Exception:
        if credential is not None:
            access_tokens.revoke(credential)
        raise
    if credential is None:
        raise authentication_failed()
    return response, credential


def platform_session(db, credential, access_tokens):
    """Sesión de plataforma. Rechaza tokens emitidos para una empresa."""
    access = access_tokens.get(credential)
    if access is None or not access.plataforma:
        raise authentication_failed()
    user = db.get(Usuario, access.usuario_id)
    if (user is None or user.tipo != "S" or user.idemp is not None
            or not secrets.compare_digest(access.password_digest, digest(user.contrasena))):
        access_tokens.revoke(credential)
        raise authentication_failed()
    return access, public_identity(db, user, access.expira_en)


def current_session(db, credential, access_tokens):
    _, identity = authenticated_session(db, credential, access_tokens)
    return identity


def authenticated_session(db, credential, access_tokens):
    access = access_tokens.get(credential)
    if access is None:
        raise authentication_failed()
    # Un token de plataforma no sirve en rutas de empresa (y viceversa).
    if access.plataforma:
        raise authentication_failed()
    if db.info.get("multitenant"):
        if access.idemp is None or not is_postgresql(db):
            raise authentication_failed()
        tenant_context(db, access.idemp)
        # Lookup by validated session tenant; no client-controlled idEmp.
        db.info["empresa"] = db.scalar(text("""SELECT e.codigo FROM empresa e WHERE e.idemp = :idemp
            AND e.estado = 'activa' AND EXISTS (SELECT 1 FROM suscripcion s
            WHERE s.idemp = e.idemp AND s.estado = 'activa'
              AND CURRENT_DATE BETWEEN s.fechainicio AND s.fechavencimiento)"""),
                                        {"idemp": access.idemp})
        if not db.info["empresa"]:
            access_tokens.revoke(credential)
            raise authentication_failed()
    user = db.get(Usuario, access.usuario_id)
    # Cambiar la contraseña invalida también accesos previos y otros dispositivos.
    if (user is None or (db.info.get("multitenant") and user.idemp != access.idemp)
            or not secrets.compare_digest(access.password_digest, digest(user.contrasena))):
        access_tokens.revoke(credential)
        raise authentication_failed()
    return access, public_identity(db, user, access.expira_en)


def logout(db, credential, peer, access_tokens):
    access = access_tokens.get(credential)
    if access is None:
        raise authentication_failed()
    with db.begin():
        if access.plataforma:
            current, _ = platform_session(db, credential, access_tokens)
        else:
            current, _ = authenticated_session(db, credential, access_tokens)
        record(db, "logout_correcto", current.usuario_id, peer, True)
    access_tokens.revoke(credential)


def request_recovery(db, data, settings, delivery, peer):
    unavailable = DomainError(503, "recuperacion_no_disponible", "La recuperación de contraseña no está disponible")
    started = monotonic()
    token = new_credential()
    token_digest = digest(token)
    try:
        with db.begin():
            idemp = resolver_idemp_empresa(db, settings, getattr(data, "empresa", None))
            if db.info.get("multitenant"):
                tenant_context(db, idemp)
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
    # by_email respeta el tenant de sesión: solo se recupera la cuenta de la empresa
    # asociada al código validado, nunca cuentas de otra empresa con el mismo correo.
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
        # El token se busca por digest SIN contexto de tenant (el cliente no
        # conoce su empresa al restablecer). En PostgreSQL usamos una función
        # SECURITY DEFINER para obtener el idemp del token y fijar el contexto
        # de tenant antes de cualquier operación protegida por RLS. En SQLite no
        # hay RLS, así que se busca directamente por digest.
        fila = None
        if is_postgresql(db):
            fila = db.execute(
                text(
                    "SELECT idemp, usuario_id FROM public.sp_recuperacion_token_por_digest(:digest)"
                ),
                {"digest": digest(data.token.get_secret_value())},
            ).mappings().first()
            if fila is None:
                raise rejected
            tenant_context(db, fila["idemp"])
            token = recovery.by_digest(db, digest(data.token.get_secret_value()))
        else:
            # En SQLite no hay RLS: buscamos el token directamente por digest.
            token = recovery.by_digest(db, digest(data.token.get_secret_value()))
            if token is None:
                raise rejected
            fila = {"idemp": token.idemp, "usuario_id": token.usuario_id}
        # Same lock order as issuance and login. Conditional consume rechecks after waiting.
        user = usuario.locked(db, token.usuario_id)
        if user is None:
            raise rejected
        # Comparación de expiración: aware en PostgreSQL (timestamptz), naive en SQLite.
        if is_postgresql(db):
            now = utcnow()
        else:
            now = utcnow().replace(tzinfo=None)
        if not recovery.consume(db, token.id, now):
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
