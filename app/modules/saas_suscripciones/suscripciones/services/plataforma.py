import json
import secrets
import unicodedata
from datetime import datetime, timedelta
from ipaddress import ip_address
from uuid import uuid4
from zoneinfo import ZoneInfo

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
       p.idPlan AS "idPlan", p.nombre AS plan, p.maxSucursales AS "maxSucursales",
       s.estado AS "suscripcionEstado", s.fechaInicio AS "fechaInicio",
       s.fechaVencimiento AS "fechaVencimiento",
       (SELECT count(*) FROM sucursal su
         WHERE su.idEmp = e.idEmp AND su.estado = 'activo') AS "sucursalesActivas"
FROM empresa e
LEFT JOIN suscripcion s ON s.idEmp = e.idEmp AND s.estado = 'activa'
LEFT JOIN plan p ON p.idPlan = s.idPlan
"""


def resumen_plataforma(db, *, incluir_auditoria=False, hoy=None):
    """Indicadores sobre el esquema compartido real, sin escribir en la BD."""
    hoy = hoy or datetime.now(ZoneInfo("America/La_Paz")).date()
    dias = 30
    parametros = {"hoy": hoy, "hasta": hoy + timedelta(days=dias)}
    empresas = db.execute(text('''
        SELECT count(*) AS total,
               coalesce(sum(CASE WHEN estado='activa' THEN 1 ELSE 0 END),0) AS activas,
               coalesce(sum(CASE WHEN estado='suspendida' THEN 1 ELSE 0 END),0) AS suspendidas,
               coalesce(sum(CASE WHEN estado='inactiva' THEN 1 ELSE 0 END),0) AS inactivas
        FROM empresa
    ''')).mappings().one()
    suscripciones = db.execute(text('''
        SELECT coalesce(sum(CASE WHEN estado='activa' AND fechainicio <= :hoy
                 AND fechavencimiento >= :hoy THEN 1 ELSE 0 END),0) AS vigentes,
               coalesce(sum(CASE WHEN estado='vencida' OR
                 (estado='activa' AND fechavencimiento < :hoy) THEN 1 ELSE 0 END),0) AS vencidas,
               coalesce(sum(CASE WHEN estado='activa' AND fechainicio <= :hoy
                 AND fechavencimiento BETWEEN :hoy AND :hasta THEN 1 ELSE 0 END),0) AS "proximasAVencer"
        FROM suscripcion
    '''), parametros).mappings().one()
    distribucion = db.execute(text('''
        SELECT p.nombre, count(*) AS suscripciones
        FROM suscripcion s JOIN plan p ON p.idplan=s.idplan
        WHERE s.estado='activa' AND s.fechainicio <= :hoy AND s.fechavencimiento >= :hoy
        GROUP BY p.idplan,p.nombre ORDER BY p.nombre
    '''), parametros).mappings().all()
    return {
        "empresas": dict(empresas), "suscripciones": dict(suscripciones),
        "distribucionPlanes": [dict(row) for row in distribucion],
        "sucursalesActivas": db.scalar(text("SELECT count(*) FROM sucursal WHERE estado='activo'")),
        "actividadReciente": listar_auditoria(db, 5) if incluir_auditoria else [],
        "diasProximosAVencer": dias,
    }


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
    has_sp = db.scalar(text(
        "SELECT to_regprocedure('public.sp_plataforma_auditar(character varying, character varying, jsonb, inet)') IS NOT NULL"
    ))
    payload = json.dumps(detalles, ensure_ascii=False)
    ip = _peer(peer)
    if has_sp:
        db.execute(text(
            "CALL sp_plataforma_auditar(CAST(:usuario AS varchar), CAST(:accion AS varchar), "
            "CAST(:detalles AS jsonb), CAST(:ip AS inet))"), {
            "usuario": usuario, "accion": accion[:150],
            "detalles": payload, "ip": ip,
        })
    else:
        db.execute(text(
            "INSERT INTO bitacora(accion, usuario_id, ip, detalles, idemp) "
            "VALUES (CAST(:accion AS varchar), CAST(:usuario AS varchar), CAST(:ip AS inet), "
            "CAST(:detalles AS jsonb), NULL)"), {
            "usuario": usuario, "accion": accion[:150],
            "detalles": payload, "ip": ip,
        })


def _asegurar_permisos_roles_empresa(db, idemp: int) -> None:
    permisos_por_rol = {
        f"E{idemp}-ADM": (
            "CU05", "CU06", "CU07", "CU08", "CU09",
            "CU15", "CU18", "CU19", "CU20", "CU21",
            "CU22", "CU23", "CU24", "CU25",
        ),
        f"E{idemp}-ENC": ("CU18", "CU19", "CU20", "CU21", "CU22", "CU23", "CU25"),
        f"E{idemp}-CAJ": ("CU15", "CU22", "CU23", "CU24"),
        f"E{idemp}-CLI": ("CU10", "CU11", "CU12", "CU13", "CU14", "CU15", "CU17"),
    }
    for nrorol, codigos in permisos_por_rol.items():
        db.execute(text("""
            INSERT INTO rol_funcion(nrorol, idfun, descripcion, idemp)
            SELECT r.nro, f.id, 'Permiso ' || f.id || ' asignado al rol ' || r.nro, r.idemp
            FROM rol r
            JOIN funcion f ON f.id = ANY(CAST(:codigos AS varchar[]))
            WHERE r.nro = :nrorol AND r.idemp = :idemp
            ON CONFLICT (nrorol, idfun) DO NOTHING
        """), {"nrorol": nrorol, "idemp": idemp, "codigos": list(codigos)})


def _prefijo_empresa(nombre: str) -> str:
    limpio = "".join(
        ch for ch in unicodedata.normalize("NFKD", nombre or "")
        if not unicodedata.combining(ch) and ch.isalnum()
    ).lower()
    return (limpio[:3] or "emp").ljust(3, "x")[:3]


def _generar_codigo_empresa(db, nombre: str, codigo_sugerido: str | None = None) -> str:
    if codigo_sugerido and codigo_sugerido.strip():
        return codigo_sugerido.strip().lower()
    existentes = {
        (row or "").strip().lower()
        for row in db.scalars(text("SELECT codigo FROM empresa"))
    }
    prefijo = _prefijo_empresa(nombre)
    for _ in range(50):
        sufijo = secrets.randbelow(9000) + 1000
        candidato = f"{prefijo}-{sufijo}"
        if candidato not in existentes:
            return candidato
    return f"{prefijo}-{uuid4().hex[:4]}"


def crear_empresa(db, data, passwords, usuario: str, peer=None) -> int:
    """Aprovisiona empresa + suscripción + roles base + administrador inicial.

    Reutiliza `sp_registrar_empresa` (no duplica la lógica en Python) y audita
    la operación en la misma transacción.
    """
    encoded = passwords.hash(data.administrador.contrasena.get_secret_value())
    id_admin = (data.administrador.idUsuario or "").strip() or str(uuid4())
    try:
        with db.begin():
            codigo = _generar_codigo_empresa(db, data.nombre, data.codigo)
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
                "codigo": codigo, "nombre": data.nombre, "nit": data.nit,
                "idPlan": data.idPlan, "inicio": data.fechaInicio, "fin": data.fechaVencimiento,
                "idAdmin": id_admin, "ci": data.administrador.ci,
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
            db.execute(text(
                "UPDATE admin SET cod_adm = 'Adm-000001' WHERE idusuario = :idAdmin AND idemp = :idemp"
            ), {"idAdmin": id_admin, "idemp": idemp})
            _asegurar_permisos_roles_empresa(db, idemp)
            auditar(db, usuario, "empresa_aprovisionada",
                    {"idEmp": idemp, "codigo": codigo, "idPlan": data.idPlan}, peer)
    except DomainError:
        raise
    except DBAPIError as exc:
        raise _domain_error(exc) from None
    return idemp


def actualizar_identidad(db, idemp: int, data, usuario: str, peer=None):
    try:
        with db.begin():
            has_sp = db.scalar(text(
                "SELECT to_regprocedure('public.sp_plataforma_identidad_visual(integer, character varying, character varying, character varying, character varying)') IS NOT NULL"
            ))
            if has_sp:
                db.execute(text(
                    "CALL sp_plataforma_identidad_visual(CAST(:idemp AS integer), CAST(:nombre AS varchar), "
                    "CAST(:logo AS varchar), CAST(:primario AS varchar), CAST(:secundario AS varchar))"), {
                    "idemp": idemp, "nombre": data.nombre, "logo": data.logoUrl,
                    "primario": data.colorPrimario, "secundario": data.colorSecundario,
                })
            else:
                obtener_empresa(db, idemp)
                if data.nombre is not None:
                    db.execute(text(
                        "UPDATE empresa SET nombre = CAST(:nombre AS varchar) WHERE idemp = :idemp"
                    ), {"idemp": idemp, "nombre": data.nombre})
            auditar(db, usuario, "empresa_identidad_actualizada", {"idEmp": idemp}, peer)
    except DomainError:
        raise
    except DBAPIError as exc:
        raise _domain_error(exc) from None
    return obtener_empresa(db, idemp)


def cambiar_estado(db, idemp: int, estado: str, usuario: str, peer=None):
    try:
        with db.begin():
            has_sp = db.scalar(text(
                "SELECT to_regprocedure('public.sp_plataforma_estado_empresa(integer, character varying)') IS NOT NULL"
            ))
            if has_sp:
                db.execute(text("CALL sp_plataforma_estado_empresa(CAST(:idemp AS integer), CAST(:estado AS varchar))"),
                           {"idemp": idemp, "estado": estado})
            else:
                obtener_empresa(db, idemp)
                db.execute(text("UPDATE empresa SET estado = CAST(:estado AS varchar) WHERE idemp = :idemp"),
                           {"idemp": idemp, "estado": estado})
            auditar(db, usuario, "empresa_estado_actualizado", {"idEmp": idemp, "estado": estado}, peer)
    except DomainError:
        raise
    except DBAPIError as exc:
        raise _domain_error(exc) from None
    return obtener_empresa(db, idemp)


def cambiar_plan(db, idemp: int, data, usuario: str, peer=None, delivery=None):
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

    try:
        from app.modules.saas_suscripciones.suscripciones.services.renovaciones import notificar_cambio_plan_empresa
        notificar_cambio_plan_empresa(db, idemp, data.idPlan, data.fechaInicio, data.fechaVencimiento, delivery=delivery)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("Error al notificar cambio de plan a empresa: %s", e)

    return obtener_empresa(db, idemp)


def uso_sucursales(db, idemp: int):
    obtener_empresa(db, idemp)
    row = db.execute(text("SELECT usadas, limite FROM fn_plataforma_uso_sucursales(CAST(:idemp AS integer))"),
                     {"idemp": idemp}).first()
    return {"usadas": row[0] if row else 0, "limite": row[1] if row else 0}


def listar_auditoria(db, limite: int = 100, desplazamiento: int = 0, accion: str = ''):
    filtro = accion.strip().lower().replace('!', '!!').replace('%', '!%').replace('_', '!_')
    rows = db.execute(text("""
        SELECT id, accion, usuario_id, fecha, detalles
        FROM bitacora WHERE idemp IS NULL
          AND lower(accion) LIKE :accion ESCAPE '!'
        ORDER BY fecha DESC, id DESC LIMIT :limite OFFSET :desplazamiento
    """), {"limite": limite, "desplazamiento": desplazamiento, "accion": '%' + filtro + '%'}).mappings().all()
    return [{"id": row["id"], "accion": row["accion"], "usuarioId": row["usuario_id"],
             "fecha": row["fecha"].isoformat() if hasattr(row["fecha"], 'isoformat') else row["fecha"],
             "detalles": json.loads(row["detalles"]) if isinstance(row["detalles"], str) else row["detalles"]} for row in rows]


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
