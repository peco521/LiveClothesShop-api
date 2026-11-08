"""Procesamiento de lotes de sincronización offline.

Reglas verificadas en PostgreSQL (base de pruebas separada):

* El tenant proviene SIEMPRE de `db.info["idemp"]`, fijado por la sesión
  autenticada. El `idEmp` que pudiera venir en el lote se ignora.
* Cada operación se ejecuta en su propia transacción: un lote puede quedar
  parcialmente aceptado.
* El registro de idempotencia (`operacion_sync`) y el efecto de negocio se
  confirman en la MISMA transacción mediante `fn_sync_tomar_operacion` +
  `sp_sync_confirmar`.
* Si el efecto de negocio falla, se revierte todo y se registra el rechazo o
  conflicto en una transacción aparte, de modo que un reintento reciba el
  mismo resultado sin volver a ejecutar la operación.
"""

import hashlib
import json
from datetime import date, timezone

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.database import sqlstate
from app.core.errors import DomainError
from app.modules.sincronizacion.schemas import (
    DatosReserva, DatosVenta, HistorialSync, OperacionHistorial, ResultadoOperacion,
)

# Códigos de error estables compartidos por web y móvil.
STOCK_INSUFICIENTE = "stock_insuficiente"
PRECIO_CAMBIADO = "precio_cambiado"
RESERVA_VENCIDA = "reserva_vencida"
DEPENDENCIA_RECHAZADA = "dependencia_rechazada"
DEPENDENCIA_PENDIENTE = "dependencia_pendiente"
CONTENIDO_INCOMPATIBLE = "contenido_incompatible"
PERMISO_DENEGADO = "permiso_denegado"
SUCURSAL_INVALIDA = "sucursal_invalida"
FUNCIONALIDAD_NO_HABILITADA = "funcionalidad_no_habilitada"
TIPO_NO_SOPORTADO = "tipo_no_soportado"
DESCUENTO_NO_AUTORIZADO = "descuento_no_autorizado"
OPERACION_RECHAZADA = "operacion_rechazada"
EN_PROCESO = "en_proceso"

# (fragmento del mensaje del procedimiento, código, estado terminal)
_REGLAS = (
    ("STOCK_INSUFICIENTE", STOCK_INSUFICIENTE, "conflicto"),
    ("No hay disponibilidad", STOCK_INSUFICIENTE, "conflicto"),
    ("PRECIO_CAMBIADO", PRECIO_CAMBIADO, "conflicto"),
    ("reserva vencida", RESERVA_VENCIDA, "conflicto"),
    ("RESERVA_VENCIDA", RESERVA_VENCIDA, "conflicto"),
    ("DESCUENTO_NO_AUTORIZADO", DESCUENTO_NO_AUTORIZADO, "conflicto"),
    ("SUCURSAL_NO_AUTORIZADA", SUCURSAL_INVALIDA, "rechazada"),
    ("SUCURSAL_INVALIDA", SUCURSAL_INVALIDA, "rechazada"),
    ("no pertenece a la empresa", SUCURSAL_INVALIDA, "rechazada"),
    ("no está activa", SUCURSAL_INVALIDA, "rechazada"),
    ("FUNCIONALIDAD_NO_HABILITADA", FUNCIONALIDAD_NO_HABILITADA, "rechazada"),
    ("TIPO_NO_SOPORTADO", TIPO_NO_SOPORTADO, "rechazada"),
    ("DATOS_INVALIDOS", OPERACION_RECHAZADA, "rechazada"),
    ("ORIGEN_INVALIDO", OPERACION_RECHAZADA, "rechazada"),
    ("UUID_REQUERIDO", OPERACION_RECHAZADA, "rechazada"),
)

PERMISOS_POR_TIPO = {"venta": "CU24", "reserva": "CU11"}


def _mensaje(exc) -> str:
    if isinstance(exc, DomainError):
        return exc.message[:200]
    raw = str(getattr(exc, "orig", exc)).strip().splitlines()
    first = raw[0] if raw else ""
    if first.startswith("ERROR:"):
        first = first[len("ERROR:"):].strip()
    if "CONTEXT:" in first:
        first = first.split("CONTEXT:")[0].strip()
    return first[:200] or "La operación no pudo completarse"


def clasificar(exc) -> tuple[str, str]:
    if isinstance(exc, DomainError):
        return exc.code, ("conflicto" if exc.code in {"disponibilidad_insuficiente", "stock_insuficiente", "reserva_vencida"} else "rechazada")
    mensaje = _mensaje(exc)
    for fragmento, codigo, estado in _REGLAS:
        if fragmento.lower() in mensaje.lower():
            return codigo, estado
    if sqlstate(exc) == "42501":
        return PERMISO_DENEGADO, "rechazada"
    return OPERACION_RECHAZADA, "rechazada"


def huella(operacion) -> str:
    """SHA-256 del contenido canónico: mismo UUID con otro contenido = conflicto."""
    canonico = json.dumps({
        "tipo": operacion.tipo, "version": operacion.version,
        "sucursal": operacion.sucursal, "datos": operacion.datos,
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(canonico.encode("utf-8")).hexdigest()


def _datos_venta(operacion) -> DatosVenta:
    try:
        return DatosVenta.model_validate(operacion.datos)
    except ValidationError:
        raise DomainError(400, "datos_invalidos", "Los datos de la venta no son válidos") from None


def _datos_reserva(operacion) -> DatosReserva:
    try:
        return DatosReserva.model_validate(operacion.datos)
    except ValidationError:
        raise DomainError(400, "datos_invalidos", "Los datos de la reserva no son válidos") from None


def _resultado(operacion, estado, *, codigo=None, detalle=None, nro_venta=None,
               nro_reserva=None, intentos=0, idempotente=False) -> ResultadoOperacion:
    return ResultadoOperacion(id=operacion.id, estado=estado, codigo=codigo, detalle=detalle,
                              nroVenta=nro_venta, nroReserva=nro_reserva,
                              intentos=intentos, idempotente=idempotente)


def procesar(db, lote, settings, identity) -> dict:
    idemp = db.info.get("idemp")
    empresa = db.info.get("empresa")
    if idemp is None or not empresa:
        raise DomainError(401, "autenticacion_rechazada", "Sincronización requiere una sesión de empresa")
    if len(lote.operaciones) > settings.sync_max_operations:
        raise DomainError(400, "lote_demasiado_grande",
                          f"El máximo por lote es {settings.sync_max_operations} operaciones")
    vistos = set()
    for operacion in lote.operaciones:
        if operacion.id in vistos:
            raise DomainError(400, "lote_invalido", "Una operación está repetida en el mismo lote")
        vistos.add(operacion.id)

    resultados: list[ResultadoOperacion] = []
    confirmadas: dict[str, str] = {}
    for operacion in lote.operaciones:
        resultados.append(_procesar_una(db, idemp, operacion, identity, lote.dispositivo,
                                        lote.origen, settings, confirmadas))
    sincronizadas = sum(1 for r in resultados if r.estado == "sincronizada")
    return {
        "empresa": empresa,
        "dispositivo": lote.dispositivo,
        "resultados": resultados,
        "sincronizadas": sincronizadas,
        "pendientes": sum(1 for r in resultados if r.estado in ("pendiente", "en_proceso")),
        "conflictos": sum(1 for r in resultados if r.estado == "conflicto"),
        "rechazadas": sum(1 for r in resultados if r.estado == "rechazada"),
    }


def _permiso(operacion, identity) -> ResultadoOperacion | None:
    requerido = PERMISOS_POR_TIPO.get(operacion.tipo)
    if requerido is None:
        return _resultado(operacion, "rechazada", codigo=TIPO_NO_SOPORTADO,
                          detalle="Tipo de operación no soportado")
    if requerido not in identity.permisos:
        return _resultado(operacion, "rechazada", codigo=PERMISO_DENEGADO,
                          detalle=f"El rol no autoriza {operacion.tipo}")
    return None


def _dependencias(db, idemp, operacion, confirmadas, cache, usuario_id=None):
    for dependencia in operacion.dependeDe:
        clave = str(dependencia)
        if usuario_id is not None:
            owner = db.scalar(text("SELECT idusuario FROM operacion_sync WHERE idemp=:tenant "
                                   "AND idoperacion=CAST(:uuid AS uuid)"),
                              {"tenant": idemp, "uuid": clave})
            if owner != usuario_id:
                return _resultado(operacion, "conflicto", codigo=DEPENDENCIA_RECHAZADA,
                                  detalle="Dependencia no disponible para esta sesión")
        if clave in confirmadas:
            if confirmadas[clave] != "confirmada":
                return _resultado(operacion, "conflicto", codigo=DEPENDENCIA_RECHAZADA,
                                  detalle="Una operación de la que depende fue rechazada")
            continue
        if clave not in cache:
            cache[clave] = db.execute(text(
                "SELECT estado FROM fn_sync_estado_operacion(CAST(:idemp AS integer), CAST(:uuid AS uuid))"),
                {"idemp": idemp, "uuid": clave}).first()
        fila = cache[clave]
        if fila is None:
            return _resultado(operacion, "conflicto", codigo=DEPENDENCIA_PENDIENTE,
                              detalle="Falta sincronizar una operación previa")
        # El servidor puede estar terminándola: el cliente reintentará.
        if fila[0] == "en_proceso":
            return _resultado(operacion, "en_proceso", codigo=EN_PROCESO,
                              detalle="Esperando la operación previa")
        if fila[0] != "confirmada":
            return _resultado(operacion, "conflicto", codigo=DEPENDENCIA_RECHAZADA,
                              detalle="Una operación de la que depende fue rechazada")
    return None


def _tomar(db, idemp, operacion, hash_payload, id_dispositivo, usuario_id) -> str:
    return db.execute(text("""
        SELECT fn_sync_tomar_operacion(
          CAST(:idemp AS integer), CAST(:uuid AS uuid), CAST(:tipo AS varchar),
          CAST(:sucursal AS integer), CAST(:usuario AS varchar),
          CAST(:fecha AS timestamptz), CAST(:payload AS jsonb), CAST(:hash AS char(64)),
          CAST(:dispositivo AS varchar), CAST(:version AS smallint))
    """), {
        "idemp": idemp, "uuid": str(operacion.id), "tipo": operacion.tipo,
        "sucursal": operacion.sucursal, "usuario": usuario_id,
        "fecha": _fecha_tz(operacion.fecha), "payload": json.dumps(operacion.datos, ensure_ascii=False),
        "hash": hash_payload, "dispositivo": id_dispositivo, "version": operacion.version,
    }).scalar_one()


def _fecha_tz(valor):
    return valor if valor.tzinfo is not None else valor.replace(tzinfo=timezone.utc)


def _estado_guardado(db, idemp, operacion_id):
    return db.execute(text("""
        SELECT estado, codigoMotivo, detalleMotivo, intentos, nroVenta, nroReserva
        FROM fn_sync_estado_operacion(CAST(:idemp AS integer), CAST(:uuid AS uuid))
    """), {"idemp": idemp, "uuid": str(operacion_id)}).first()


def _procesar_una(db, idemp, operacion, identity, id_dispositivo, origen, settings, confirmadas):
    denegado = _permiso(operacion, identity)
    if denegado is not None:
        return denegado

    usuario_id = identity.usuario.idUsuario
    db.rollback()
    if operacion.dependeDe:
        problema = _dependencias(db, idemp, operacion, confirmadas, {},
                                 usuario_id if getattr(identity.usuario, "tipo", None) == "C" else None)
        if problema is not None:
            return problema
        db.rollback()

    hash_payload = huella(operacion)
    estado = None
    fila = None
    try:
        with db.begin():
            # Serialize HTTP claims before checking ownership. A guessed UUID
            # must not reveal another client's result or update its ledger.
            db.execute(text("SELECT pg_advisory_xact_lock(:tenant, hashtext(:uuid))"),
                       {"tenant": idemp, "uuid": str(operacion.id)})
            owner = db.scalar(text("SELECT idusuario FROM operacion_sync "
                                   "WHERE idemp=:tenant AND idoperacion=CAST(:uuid AS uuid)"),
                              {"tenant": idemp, "uuid": str(operacion.id)})
            if owner is not None and owner != usuario_id:
                return _resultado(operacion, "rechazada", codigo=PERMISO_DENEGADO,
                                  detalle="La operación no pertenece a esta sesión")
            estado = _tomar(db, idemp, operacion, hash_payload, id_dispositivo, usuario_id)
            if estado == "nueva":
                nro_venta, nro_reserva = _ejecutar(db, idemp, operacion, identity, origen)
                db.execute(text(
                    "CALL sp_sync_confirmar(CAST(:idemp AS integer), CAST(:uuid AS uuid), "
                    "CAST(:venta AS integer), CAST(:reserva AS integer))"), {
                    "idemp": idemp, "uuid": str(operacion.id),
                    "venta": nro_venta, "reserva": nro_reserva,
                })
                confirmadas[str(operacion.id)] = "confirmada"
                return _resultado(operacion, "sincronizada", nro_venta=nro_venta,
                                  nro_reserva=nro_reserva, intentos=1)
            if estado == "en_proceso":
                confirmadas[str(operacion.id)] = "en_proceso"
                return _resultado(operacion, "en_proceso", codigo=EN_PROCESO,
                                  detalle="La operación está siendo procesada; reintente")
            fila = _estado_guardado(db, idemp, operacion.id)
    except (DBAPIError, DomainError) as exc:
        db.rollback()
        if estado == "nueva":
            # El efecto de negocio falló: se revirtió el registro de idempotencia.
            # Queda como conflicto/rechazo terminal en una transacción aparte.
            codigo, terminal = clasificar(exc)
            detalle = _mensaje(exc)
            _registrar_rechazo(db, idemp, operacion, identity, id_dispositivo,
                               hash_payload, terminal, codigo, detalle)
            confirmadas[str(operacion.id)] = terminal
            return _resultado(operacion, terminal, codigo=codigo, detalle=detalle, intentos=1)
        if isinstance(exc, DomainError):
            raise
        raise

    if estado == "conflicto_contenido":
        return _resultado(operacion, "conflicto", codigo=CONTENIDO_INCOMPATIBLE,
                          detalle="El mismo identificador ya se usó con otro contenido",
                          idempotente=True)
    if estado == "ya_confirmada":
        confirmadas[str(operacion.id)] = "confirmada"
        return _resultado(operacion, "sincronizada",
                          nro_venta=fila[4] if fila else None,
                          nro_reserva=fila[5] if fila else None,
                          intentos=fila[3] if fila else 0, idempotente=True)
    if estado in ("rechazada", "conflicto"):
        confirmadas[str(operacion.id)] = estado
        return _resultado(operacion, estado, codigo=fila[1] if fila else None,
                          detalle=fila[2] if fila else None,
                          nro_venta=fila[4] if fila else None,
                          nro_reserva=fila[5] if fila else None,
                          intentos=fila[3] if fila else 0, idempotente=True)
    # Estado inesperado: nunca confirmar en silencio.
    return _resultado(operacion, "pendiente", codigo=OPERACION_RECHAZADA,
                      detalle="Estado de sincronización desconocido; reintente")


def _ejecutar(db, idemp, operacion, identity, origen) -> tuple[int | None, int | None]:
    if operacion.tipo == "venta":
        return _venta(db, idemp, operacion, identity, origen), None
    return None, _reserva(db, idemp, operacion, identity, origen)


def _venta(db, idemp, operacion, identity, origen) -> int:
    datos = _datos_venta(operacion)
    lineas = datos.lineas
    resultado = db.execute(text("""
        CALL sp_registrar_venta_offline(
          CAST(:idemp AS integer), CAST(:sucursal AS integer),
          CAST(:usuario AS varchar), CAST(:cliente AS varchar),
          CAST(:variantes AS varchar[]), CAST(:cantidades AS integer[]), CAST(:precios AS numeric[]),
          CAST(:uuid AS uuid), CAST(:origen AS varchar), CAST(:metodo AS varchar),
          CAST(:descuento AS numeric), CAST(:referencia AS varchar), NULL)
    """), {
        "idemp": idemp, "sucursal": operacion.sucursal,
        "usuario": identity.usuario.idUsuario,
        # Una venta de caja puede ser anónima. No convertir al cajero en
        # cliente: la FK compuesta solo admite clientes del mismo tenant.
        "cliente": datos.idCliente,
        "variantes": [linea.idVariante for linea in lineas],
        "cantidades": [linea.cantidad for linea in lineas],
        "precios": [linea.precio for linea in lineas],
        "uuid": str(operacion.id), "origen": origen, "metodo": datos.metodo,
        "descuento": datos.descuento, "referencia": datos.referencia,
    })
    fila = resultado.fetchone()
    if not fila or fila[0] is None:
        raise DomainError(409, "venta_no_registrada", "La venta no pudo registrarse")
    return int(fila[0])


def _reserva(db, idemp, operacion, identity, origen) -> int:
    from app.modules.cliente_experiencia_compra.shared.dependencies import require_cliente
    from app.modules.cliente_experiencia_compra.cu11_gestionar_reserva.services import reserva as service
    from app.modules.cliente_experiencia_compra.cu11_gestionar_reserva.schemas.reserva import ReservaCrear
    from app.modules.seguridad_accesos.services.bitacora import record

    require_cliente(identity, db)
    datos = _datos_reserva(operacion)
    reservation = ReservaCrear(nroSuc=operacion.sucursal, fechaReserva=datos.fecha,
        horaAtencion=datos.hora, items=[{"idVar": row.idVariante, "cantidad": row.cantidad} for row in datos.lineas])
    if reservation.fechaReserva < date.today():
        raise DomainError(409, RESERVA_VENCIDA, "La fecha de la reserva ya venció")
    merged = service._combinar_items(reservation.items)
    service._sucursal(db, operacion.sucursal)
    service._validar_horario(db, operacion.sucursal, reservation.horaAtencion, reservation.fechaReserva)
    service._validar_variantes(db, merged)
    resultado = db.execute(text("""
        CALL sp_realizar_reserva(
          CAST(:idemp AS integer), CAST(:usuario AS varchar), CAST(:sucursal AS integer),
          CAST(:fecha AS date), CAST(:hora AS time),
          CAST(:variantes AS varchar[]), CAST(:cantidades AS integer[]),
          NULL, CAST(:uuid AS uuid), CAST(:origen AS varchar))
    """), {
        "idemp": idemp, "usuario": identity.usuario.idUsuario,
        "sucursal": operacion.sucursal, "fecha": datos.fecha, "hora": datos.hora,
        "variantes": sorted(merged),
        "cantidades": [merged[key] for key in sorted(merged)],
        "uuid": str(operacion.id),
        "origen": origen,
    })
    fila = resultado.fetchone()
    if not fila or fila[0] is None:
        raise DomainError(409, "reserva_no_registrada", "La reserva no pudo registrarse")
    record(db, "reserva_creada", identity.usuario.idUsuario, None, True)
    return int(fila[0])


def _registrar_rechazo(db, idemp, operacion, identity, id_dispositivo,
                       hash_payload, estado, codigo, detalle):
    with db.begin():
        db.execute(text("""
            CALL sp_sync_registrar_rechazo(
              CAST(:idemp AS integer), CAST(:uuid AS uuid), CAST(:tipo AS varchar),
              CAST(:sucursal AS integer), CAST(:usuario AS varchar),
              CAST(:fecha AS timestamptz), CAST(:payload AS jsonb), CAST(:hash AS char(64)),
              CAST(:dispositivo AS varchar), CAST(:version AS smallint),
              CAST(:estado AS varchar), CAST(:codigo AS varchar), CAST(:detalle AS text))
        """), {
            "idemp": idemp, "uuid": str(operacion.id), "tipo": operacion.tipo,
            "sucursal": operacion.sucursal, "usuario": identity.usuario.idUsuario,
            "fecha": _fecha_tz(operacion.fecha),
            "payload": json.dumps(operacion.datos, ensure_ascii=False),
            "hash": hash_payload, "dispositivo": id_dispositivo, "version": operacion.version,
            "estado": estado, "codigo": codigo, "detalle": detalle,
        })


def historial(db, estado: str | None, offset: int, limit: int, identity=None) -> HistorialSync:
    idemp = db.info.get("idemp")
    if idemp is None:
        raise DomainError(401, "autenticacion_rechazada", "Sincronización requiere una sesión de empresa")
    filtro = " AND estado = :estado" if estado else ""
    params = {"idemp": idemp, "offset": offset, "limit": limit}
    if identity is not None and identity.usuario.tipo == "C":
        filtro += " AND idusuario = :user"
        params["user"] = identity.usuario.idUsuario
    if estado:
        params["estado"] = estado
    total = db.execute(text(
        f"SELECT count(*) FROM operacion_sync WHERE idEmp = :idemp{filtro}"), params).scalar_one()
    filas = db.execute(text(f"""
        SELECT idOperacion, tipo, estado, intentos, fechaLocal, fechaRecepcion,
               fechaResolucion, codigoMotivo, detalleMotivo, nroVenta, nroReserva
        FROM operacion_sync WHERE idEmp = :idemp{filtro}
        ORDER BY fechaRecepcion DESC, idOperacion
        OFFSET :offset LIMIT :limit
    """), params).all()
    return HistorialSync(total=total, operaciones=[
        OperacionHistorial(id=fila[0], tipo=fila[1], estado=fila[2], intentos=fila[3],
                           fechaLocal=fila[4], fechaRecepcion=fila[5], fechaResolucion=fila[6],
                           codigoMotivo=fila[7], detalleMotivo=fila[8],
                           nroVenta=fila[9], nroReserva=fila[10])
        for fila in filas])


def resolver_conflicto(db, operacion_id, detalle, identity):
    idemp = db.info.get("idemp")
    if idemp is None:
        raise DomainError(401, "autenticacion_rechazada", "Sincronización requiere una sesión de empresa")
    if "CU22" not in identity.permisos and "CU24" not in identity.permisos:
        raise DomainError(403, "acceso_denegado", "El rol no autoriza resolver conflictos")
    try:
        # current_identity abre una transacción para validar la sesión. Resolver
        # debe comenzar su propia transacción explícita para la mutación auditada.
        db.rollback()
        with db.begin():
            db.execute(text(
                "CALL sp_resolver_conflicto_sync(CAST(:idemp AS integer), CAST(:uuid AS uuid), "
                "CAST(:responsable AS varchar), CAST('rechazada' AS varchar), CAST(:detalle AS text))"), {
                "idemp": idemp, "uuid": str(operacion_id),
                "responsable": identity.usuario.idUsuario, "detalle": detalle,
            })
    except DBAPIError as exc:
        raise DomainError(409, "conflicto_no_resuelto", _mensaje(exc)) from None
    return {"id": operacion_id, "estado": "rechazada", "detalle": detalle}
