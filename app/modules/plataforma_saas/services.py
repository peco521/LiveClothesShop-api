import json
from ipaddress import ip_address
from datetime import date, timedelta

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.database import sqlstate
from app.core.errors import DomainError


def _safe_message(exc) -> str:
    """Primera línea del error del servidor, sin parámetros ni contexto SQL."""
    raw = str(getattr(exc, "orig", exc)).strip().splitlines()
    first = raw[0] if raw else ""
    if first.startswith("ERROR:"):
        first = first[len("ERROR:"):].strip()
    # Sin detalles de fila, sentencia ni parámetros.
    return first[:200] or "La operación no pudo completarse"


def _domain_error(exc):
    state = sqlstate(exc)
    message = _safe_message(exc)
    if state == "23505":
        return DomainError(409, "conflicto", message)
    if state == "23503":
        return DomainError(400, "referencia_invalida", message)
    if state == "23514":
        return DomainError(400, "valor_invalido", message)
    if state and state.startswith("42"):
        return DomainError(400, "operacion_invalida", message)
    return DomainError(400, "operacion_rechazada", message)


def _peer(peer) -> str | None:
    try:
        return str(ip_address(peer)) if peer else None
    except ValueError:
        return None


EMPRESA_QUERY = """
SELECT e.idEmp AS "idEmp", e.codigo, e.nombre, e.estado,
       e.logoUrl AS "logoUrl", e.colorPrimario AS "colorPrimario",
       e.colorSecundario AS "colorSecundario", e.esDemostracion AS "esDemostracion",
       p.idPlan AS "idPlan", p.nombre AS plan,
       CASE
         WHEN s.estado = 'activa'
          AND current_date BETWEEN s.fechaInicio AND s.fechaVencimiento
         THEN fn_max_sucursales(e.idEmp)
         ELSE p.maxSucursales
       END AS "maxSucursales",
       s.estado AS "suscripcionEstado", s.fechaInicio AS "fechaInicio",
       s.fechaVencimiento AS "fechaVencimiento",
       (SELECT count(*)::integer FROM sucursal su
         WHERE su.idEmp = e.idEmp AND su.estado = 'activo') AS "sucursalesActivas"
FROM empresa e
LEFT JOIN suscripcion s ON s.idEmp = e.idEmp AND s.estado = 'activa'
LEFT JOIN plan p ON p.idPlan = s.idPlan
"""


def listar_empresas(db):
    rows = db.execute(text(EMPRESA_QUERY + " ORDER BY e.idEmp")).mappings().all()
    return [dict(row) for row in rows]


def obtener_empresa(db, idemp: int):
    row = db.execute(text(EMPRESA_QUERY + " WHERE e.idEmp = :idemp"), {"idemp": idemp}).mappings().first()
    if row is None:
        raise DomainError(404, "empresa_no_encontrada", "La empresa no existe")
    return dict(row)


def listar_planes(db):
    planes = db.execute(text(
        'SELECT idPlan AS "idPlan", nombre, maxSucursales AS "maxSucursales", estado '
        'FROM plan ORDER BY idPlan')).mappings().all()
    funcionalidades: dict[int, list[str]] = {}
    for row in db.execute(text(
            "SELECT idPlan, codigo FROM plan_funcionalidad ORDER BY idPlan, codigo")):
        funcionalidades.setdefault(row[0], []).append(row[1])
    return [{**dict(plan), "funcionalidades": funcionalidades.get(plan["idPlan"], [])}
            for plan in planes]


def auditar(db, usuario: str, accion: str, detalles: dict, peer=None):
    db.execute(text(
        "CALL sp_plataforma_auditar(CAST(:usuario AS varchar), CAST(:accion AS varchar), "
        "CAST(:detalles AS jsonb), CAST(:ip AS inet))"), {
        "usuario": usuario, "accion": accion[:150],
        "detalles": json.dumps(detalles, ensure_ascii=False), "ip": _peer(peer),
    })


def crear_empresa(db, data, passwords, usuario: str, peer=None) -> int:
    """Aprovisiona empresa + suscripción + roles base + administrador inicial.

    Reutiliza `sp_registrar_empresa` (no duplica la lógica en Python) y audita
    la operación en la misma transacción.
    """
    encoded = passwords.hash(data.administrador.contrasena.get_secret_value())
    try:
        with db.begin():
            result = db.execute(text("""
                CALL sp_registrar_empresa(
                  CAST(:codigo AS varchar), CAST(:nombre AS varchar), CAST(:nit AS varchar),
                  CAST(:idPlan AS smallint), CAST(:inicio AS date), CAST(:fin AS date),
                  CAST(:idAdmin AS varchar), CAST(:ci AS varchar), CAST(:nombreAdmin AS varchar),
                  CAST(:apellidoPat AS varchar), CAST(:apellidoMat AS varchar),
                  CAST(:sexo AS char), CAST(:correo AS varchar), CAST(:telefono AS varchar),
                  CAST(:direccion AS varchar), CAST(:hash AS varchar), CAST(:fechaNac AS date),
                  NULL)
            """), {
                "codigo": data.codigo, "nombre": data.nombre, "nit": data.nit,
                "idPlan": data.idPlan, "inicio": data.fechaInicio, "fin": data.fechaVencimiento,
                "idAdmin": data.administrador.idUsuario, "ci": data.administrador.ci,
                "nombreAdmin": data.administrador.nombre,
                "apellidoPat": data.administrador.apellidoPat,
                "apellidoMat": data.administrador.apellidoMat, "sexo": data.administrador.sexo,
                "correo": str(data.administrador.correo), "telefono": data.administrador.telefono,
                "direccion": data.administrador.direccion, "hash": encoded,
                "fechaNac": data.administrador.fechaNac,
            })
            row = result.fetchone()
            idemp = row[0] if row else None
            if not idemp:
                raise DomainError(409, "empresa_no_creada", "La empresa no pudo registrarse")
            auditar(db, usuario, "empresa_aprovisionada",
                    {"idEmp": idemp, "codigo": data.codigo, "idPlan": data.idPlan}, peer)
    except DomainError:
        raise
    except DBAPIError as exc:
        raise _domain_error(exc) from None
    return idemp


def actualizar_identidad(db, idemp: int, data, usuario: str, peer=None):
    try:
        with db.begin():
            db.execute(text(
                "CALL sp_plataforma_identidad_visual(CAST(:idemp AS integer), CAST(:nombre AS varchar), "
                "CAST(:logo AS varchar), CAST(:primario AS varchar), CAST(:secundario AS varchar))"), {
                "idemp": idemp, "nombre": data.nombre, "logo": data.logoUrl,
                "primario": data.colorPrimario, "secundario": data.colorSecundario,
            })
            auditar(db, usuario, "empresa_identidad_actualizada", {"idEmp": idemp}, peer)
    except DBAPIError as exc:
        raise _domain_error(exc) from None
    return obtener_empresa(db, idemp)


def cambiar_estado(db, idemp: int, estado: str, usuario: str, peer=None):
    try:
        with db.begin():
            db.execute(text("CALL sp_plataforma_estado_empresa(CAST(:idemp AS integer), CAST(:estado AS varchar))"),
                       {"idemp": idemp, "estado": estado})
            auditar(db, usuario, "empresa_estado_actualizado", {"idEmp": idemp, "estado": estado}, peer)
    except DBAPIError as exc:
        raise _domain_error(exc) from None
    return obtener_empresa(db, idemp)


def cambiar_plan(db, idemp: int, data, usuario: str, peer=None):
    try:
        with db.begin():
            db.execute(text(
                "CALL sp_cambiar_plan(CAST(:idemp AS integer), CAST(:idPlan AS smallint), "
                "CAST(:inicio AS date), CAST(:fin AS date))"), {
                "idemp": idemp, "idPlan": data.idPlan,
                "inicio": data.fechaInicio, "fin": data.fechaVencimiento,
            })
            auditar(db, usuario, "empresa_plan_cambiado", {"idEmp": idemp, "idPlan": data.idPlan}, peer)
    except DBAPIError as exc:
        raise _domain_error(exc) from None
    return obtener_empresa(db, idemp)


def uso_sucursales(db, idemp: int):
    obtener_empresa(db, idemp)
    row = db.execute(text("SELECT usadas, limite FROM fn_plataforma_uso_sucursales(CAST(:idemp AS integer))"),
                     {"idemp": idemp}).first()
    return {"usadas": row[0] if row else 0, "limite": row[1] if row else 0}


def listar_auditoria(db, limite: int = 100, desplazamiento: int = 0,
                     accion: str | None = None):
    rows = db.execute(text("""
        SELECT id, accion, usuario_id, fecha, detalles
        FROM bitacora
        WHERE idemp IS NULL
          AND (CAST(:accion AS varchar) IS NULL OR accion = CAST(:accion AS varchar))
        ORDER BY fecha DESC, id DESC LIMIT :limite OFFSET :desplazamiento
    """), {"limite": limite, "desplazamiento": desplazamiento, "accion": accion}).mappings().all()
    return [{"id": row["id"], "accion": row["accion"], "usuarioId": row["usuario_id"],
             "fecha": row["fecha"].isoformat(), "detalles": _sanear_detalles_auditoria(row["detalles"])}
            for row in rows]


def _sanear_detalles_auditoria(value):
    """Publica solo atributos técnicos aprobados; omite texto libre y secretos."""
    if not isinstance(value, dict):
        return None
    allowed = {"idEmp", "codigo", "idPlan", "estado", "idRespaldo", "destino",
               "enabled", "hora", "zona_horaria", "tipo"}
    return {
        key: item
        for key, item in value.items()
        if key in allowed and isinstance(item, (str, int, bool, type(None)))
        and (not isinstance(item, str) or len(item) <= 150)
    }


def obtener_resumen(db, incluir_auditoria: bool = False):
    hoy = date.today()
    limite_proximo = hoy + timedelta(days=30)
    empresas = db.execute(text("""
        SELECT count(*) AS total,
               count(*) FILTER (WHERE estado = 'activa') AS activas,
               count(*) FILTER (WHERE estado = 'suspendida') AS suspendidas,
               count(*) FILTER (WHERE estado = 'inactiva') AS inactivas
        FROM empresa
    """)).mappings().one()
    suscripciones = db.execute(text("""
        SELECT count(*) FILTER (
                   WHERE estado = 'activa'
                     AND fechaInicio <= :hoy AND fechaVencimiento >= :hoy
               ) AS vigentes,
               count(*) FILTER (
                   WHERE estado = 'vencida'
                      OR (estado = 'activa' AND fechaVencimiento < :hoy)
               ) AS vencidas,
               count(*) FILTER (
                   WHERE estado = 'activa' AND fechaVencimiento BETWEEN :hoy AND :limite
               ) AS proximasAVencer
        FROM suscripcion
    """), {"hoy": hoy, "limite": limite_proximo}).mappings().one()
    planes = db.execute(text("""
        SELECT p.nombre, count(*) AS suscripciones
        FROM suscripcion s
        JOIN plan p ON p.idPlan = s.idPlan
        WHERE s.estado = 'activa'
          AND s.fechaInicio <= :hoy AND s.fechaVencimiento >= :hoy
        GROUP BY p.idPlan, p.nombre
        ORDER BY p.nombre
    """), {"hoy": hoy}).mappings().all()
    sucursales = db.execute(text(
        "SELECT count(*) FROM sucursal WHERE estado = 'activo'")).scalar_one()
    return {
        "empresas": dict(empresas),
        "suscripciones": dict(suscripciones),
        "distribucionPlanes": [dict(row) for row in planes],
        "sucursalesActivas": sucursales,
        "actividadReciente": listar_auditoria(db, 5) if incluir_auditoria else [],
        "diasProximosAVencer": 30,
    }


def resumen_soporte(db, idemp: int, motivo: str, usuario: str, peer=None):
    """Lectura acotada de una empresa, autorizada y auditada.

    No abre una sesión de la empresa ni acepta un idEmp de una sesión de
    empresa: el administrador SaaS sigue siendo plataforma.
    """
    with db.begin():
        empresa = obtener_empresa(db, idemp)
        auditar(db, usuario, "soporte_acceso_empresa",
                {"idEmp": idemp, "motivo": motivo[:300]}, peer)
        sucursales = [dict(row) for row in db.execute(text(
            "SELECT nro, nombre, estado FROM sucursal WHERE idEmp = :idemp ORDER BY nro"),
            {"idemp": idemp}).mappings()]
        usuarios = [dict(row) for row in db.execute(text(
            'SELECT u.idUsuario AS "idUsuario", u.correo, u.tipo, u.nroRol AS "nroRol" '
            "FROM usuario u WHERE u.idEmp = :idemp ORDER BY u.idUsuario"),
            {"idemp": idemp}).mappings()]
    return {"empresa": empresa, "sucursales": sucursales, "usuarios": usuarios,
            "aviso": "Acceso de solo lectura y auditado. No se emitió una sesión de la empresa."}
