import secrets
from time import monotonic, sleep
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.database import is_postgresql, tenant_context
from app.core.errors import DomainError
from app.core.security import digest, new_credential, utcnow
from app.modules.seguridad_accesos.models import RecuperacionContrasena, Usuario
from app.modules.seguridad_accesos.repositories import rol, usuario
from app.modules.seguridad_accesos.repositories import recuperacion_contrasena as recovery
from app.modules.seguridad_accesos.schemas.auth import (
    AuthResponse, MiSuscripcionResponse, RegistroResponse, RolResponse, UsuarioResponse,
)
from app.modules.seguridad_accesos.services.bitacora import record


def authentication_failed():
    return DomainError(401, "autenticacion_rechazada", "No se pudo autenticar la solicitud")


def _resolve_empresa(db, codigo: str) -> int | None:
    has_fn = db.scalar(
        text("SELECT to_regprocedure('public.auth_resolver_empresa(character varying)') IS NOT NULL")
    )
    if has_fn:
        return db.scalar(text("SELECT auth_resolver_empresa(:codigo)"), {"codigo": codigo})
    return db.scalar(
        text("""
            SELECT e.idemp FROM public.empresa e
            WHERE e.codigo = :codigo AND e.estado = 'activa'
              AND EXISTS (SELECT 1 FROM public.suscripcion s
                          WHERE s.idemp = e.idemp AND s.estado = 'activa'
                            AND CURRENT_DATE BETWEEN s.fechainicio AND s.fechavencimiento)
        """),
        {"codigo": codigo},
    )


def list_login_companies(db) -> list[dict[str, str]]:
    if not is_postgresql(db):
        return []
    rows = db.execute(
        text("""
            SELECT e.codigo, e.nombre
            FROM public.empresa e
            WHERE e.estado = 'activa'
              AND EXISTS (SELECT 1 FROM public.suscripcion s
                          WHERE s.idemp = e.idemp AND s.estado = 'activa'
                            AND CURRENT_DATE BETWEEN s.fechainicio AND s.fechavencimiento)
            ORDER BY e.nombre, e.codigo
        """)
    ).mappings().all()
    return [{"codigo": row["codigo"], "nombre": row["nombre"]} for row in rows]


def _resolve_tenant_client_role(db, settings, tenant_id: int):
    from sqlalchemy import select
    for candidate_id in (f"E{tenant_id}-CLI", settings.cliente_rol_id):
        role = rol.get(db, candidate_id)
        if (role is not None and role.idemp == tenant_id and role.estado == "activo"
                and set(rol.permissions(db, role.nro)).issubset(
                    {"CU10", "CU11", "CU12", "CU13", "CU14", "CU15", "CU17"})
                and not db.scalar(select(Usuario.idusuario).where(
                    Usuario.idemp == tenant_id, Usuario.nrorol == role.nro,
                    Usuario.tipo != "C").limit(1))):
            return role
    return None


def register(db, data, settings, passwords, peer, access_tokens):
    credential = None
    email = str(data.correo)
    # Prepare the configured password representation before the write transaction.
    encoded = passwords.hash(data.contrasena.get_secret_value())
    user_id = str(uuid4())
    try:
        with db.begin():
            idemp = None
            role_id = settings.cliente_rol_id
            if settings.multitenant_enabled:
                if not is_postgresql(db):
                    raise DomainError(503, "registro_no_disponible", "El registro no está disponible")
                codigo_emp = data.empresa
                if not codigo_emp:
                    companies = list_login_companies(db)
                    if len(companies) == 1:
                        codigo_emp = companies[0]["codigo"]
                    else:
                        raise DomainError(422, "empresa_requerida", "Selecciona la empresa en la que deseas registrarte")
                idemp = _resolve_empresa(db, codigo_emp)
                if idemp is None:
                    raise DomainError(503, "registro_no_disponible", "La empresa seleccionada no está disponible")
                tenant_context(db, idemp)
                db.info["empresa"] = codigo_emp
                if usuario.by_email(db, email):
                    raise DomainError(409, "correo_duplicado", "El correo ya está registrado")
                tenant_role = _resolve_tenant_client_role(db, settings, idemp)
                if tenant_role is None:
                    raise DomainError(503, "registro_no_disponible", "El registro no está disponible")
                role_id = tenant_role.nro
            else:
                if usuario.by_email(db, email):
                    raise DomainError(409, "correo_duplicado", "El correo ya está registrado")
                if rol.public_role(db, settings.cliente_rol_id) is None:
                    raise DomainError(503, "registro_no_disponible", "El registro no está disponible")
            client_code = secrets.token_hex(5)
            usuario.create_client(db, user_id=user_id, data=data, password_hash=encoded,
                                  role_id=role_id, client_code=client_code, idemp=idemp)
            record(db, "cliente_registrado", user_id, peer, True)
            # CU01: el registro público abre sesión con el mismo mecanismo que el
            # login (token en memoria + huella de la contraseña) y su misma auditoría.
            account = db.get(Usuario, user_id)
            if account is None:
                raise DomainError(503, "registro_no_disponible", "No se pudo completar el registro")
            expires = utcnow() + timedelta(hours=settings.session_hours)
            response = RegistroResponse(idUsuario=user_id, correo=email,
                                        sesion=public_identity(db, account, expires))
            credential = access_tokens.issue(account.idusuario, account.contrasena, expires, db.info.get("idemp"))
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
                # Pre-tenant lookup via restricted SECURITY DEFINER function or active tenant query.
                idemp = _resolve_empresa(db, data.empresa)
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


def company_subscription_summary(db, credential, access_tokens) -> MiSuscripcionResponse | None:
    access, _ = authenticated_session(db, credential, access_tokens)
    if not db.info.get("multitenant") or not is_postgresql(db) or not access.idemp:
        return None
    row = db.execute(text("""
        SELECT e.codigo AS "empresaCodigo", e.nombre AS "empresaNombre",
               p.idplan AS "idPlan", p.nombre AS plan,
               COALESCE(p.maxsucursales, 0) AS "maxSucursales",
               s.estado AS "estadoSuscripcion",
               s.fechainicio AS "fechaInicio",
               s.fechavencimiento AS "fechaVencimiento",
               (SELECT count(*) FROM sucursal su
                 WHERE su.idemp = e.idemp AND su.estado = 'activo') AS "sucursalesActivas"
        FROM empresa e
        LEFT JOIN suscripcion s ON s.idemp = e.idemp AND s.estado = 'activa'
        LEFT JOIN plan p ON p.idplan = s.idplan
        WHERE e.idemp = :idemp
    """), {"idemp": access.idemp}).mappings().first()
    if row is None:
        return None
    funcionalidades: list[str] = []
    if row["idPlan"] is not None:
        funcionalidades = list(db.scalars(text(
            "SELECT codigo FROM plan_funcionalidad WHERE idplan = :idplan ORDER BY codigo"
        ), {"idplan": row["idPlan"]}))
    return MiSuscripcionResponse(
        empresaCodigo=row["empresaCodigo"],
        empresaNombre=row["empresaNombre"],
        plan=row["plan"],
        estadoSuscripcion=row["estadoSuscripcion"],
        fechaInicio=row["fechaInicio"],
        fechaVencimiento=row["fechaVencimiento"],
        sucursalesActivas=int(row["sucursalesActivas"] or 0),
        maxSucursales=int(row["maxSucursales"] or 0),
        funcionalidades=funcionalidades,
    )


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
