"""CU11 Gestionar Reserva: crea/cancela con reserva temporal de disponibilidad.

Semántica de inventario: stock = existencia física (nunca se toca aquí);
cantDisp = disponible. Crear disminuye cantDisp; cancelar/vencer la devuelve.
Todo ocurre en una sola transacción con filas de inventario bloqueadas.
"""

from datetime import date, datetime, timedelta

from contextlib import contextmanager

from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy import select

from app.core.database import is_postgresql, sqlstate
from app.core.errors import DomainError
from app.modules.cliente_experiencia_compra.shared.horarios import leer_dias
from app.modules.cliente_experiencia_compra.cu11_gestionar_reserva.repositories import reserva as repository
from app.modules.cliente_experiencia_compra.cu11_gestionar_reserva.schemas.reserva import (
    HorarioRango,
    HorariosSucursal,
    ReservaCrear,
    ReservaDetalle,
    ReservaItemDetalle,
    ReservaSucursal,
    ReservasFiltros,
    ReservasListado,
    SucursalCliente,
    SucursalesClienteListado,
)
from app.modules.cliente_experiencia_compra.shared.models.comercio import DetalleReserva, Reserva
from app.modules.cliente_experiencia_compra.shared.repositories import comercio
from app.modules.cliente_experiencia_compra.shared.repositories import catalogo
from app.modules.cliente_experiencia_compra.shared.models.catalogo import Categoria
from app.modules.seguridad_accesos.services.bitacora import record
from app.modules.seguridad_accesos.shared.repositories import organizacion


@contextmanager
def transaction(db):
    # require_cliente ya abrió esta transacción en la sesión compartida.
    # Datos y auditoría se confirman juntos; cualquier error revierte todo.
    try:
        yield
        db.commit()
    except IntegrityError:
        db.rollback()
        raise DomainError(409, "conflicto_integridad", "Los datos entran en conflicto con un registro existente") from None
    except DBAPIError as exc:
        db.rollback()
        if sqlstate(exc) == "P0001":
            raise DomainError(409, "operacion_reserva_rechazada",
                              "La reserva no pudo completarse con la disponibilidad actual") from None
        raise
    except Exception:
        db.rollback()
        raise


def _sucursal(db, nroSuc):
    branch = organizacion.branch(db, nroSuc, lock=True)
    if branch is None:
        raise DomainError(404, "sucursal_no_encontrada", "Sucursal no encontrada")
    if branch.estado != "activo":
        raise DomainError(409, "sucursal_inactiva", "La sucursal no está disponible")
    return branch


def _validar_horario(db, nroSuc, hora, fecha=None):
    """Exige hora dentro de los rangos declarados, solo si existen.

    Sin filas en horario_suc no puede determinarse inequívocamente si la
    sucursal atiende: no se valida y se reporta como limitación.
    """
    rangos = comercio.horarios_sucursal(db, nroSuc)
    if not rangos:
        return None
    if not any(r.horaini <= hora <= r.horafin and (fecha is None or fecha.isoweekday() in leer_dias(r.dias)) for r in rangos):
        raise DomainError(422, "horario_fuera_atencion",
                          "El horario solicitado está fuera de la atención de la sucursal")
    return rangos


def _combinar_items(items):
    """Fusiona variantes duplicadas sumando cantidades (explícito y probado)."""
    merged: dict[str, int] = {}
    for item in items:
        merged[item.idVar.strip()] = merged.get(item.idVar.strip(), 0) + item.cantidad
    if any(not var for var in merged):
        raise DomainError(422, "datos_invalidos", "Los datos enviados no son válidos")
    return merged


def _validar_variantes(db, merged: dict[str, int]):
    found = repository.variantes_productos(db, sorted(merged))
    for var_id, cantidad in merged.items():
        pair = found.get(var_id)
        if pair is None:
            raise DomainError(404, "variante_no_encontrada", "Variante no encontrada")
        variant, product = pair
        if variant.estado != "activo":
            raise DomainError(404, "variante_no_encontrada", "Variante no encontrada")
        if product.estado != "activo":
            raise DomainError(404, "producto_no_encontrado", "Prenda no encontrada")
    return found


def _reservar_disponibilidad(db, nroSuc, merged: dict[str, int]):
    """Bloquea inventarios, revalida cantDisp y descuenta. Rollback total si falta."""
    for var_id in sorted(merged):
        cantidad = merged[var_id]
        row = comercio.locked_inventario(db, var_id, nroSuc)
        disponible = row.cantdisp if row is not None else 0
        if disponible < cantidad:
            raise DomainError(409, "disponibilidad_insuficiente",
                              "No hay disponibilidad suficiente para una de las prendas")
        row.cantdisp = disponible - cantidad
        if row.cantdisp < 0:  # Defensa en profundidad; el chequeo previo bajo lock lo impide.
            raise DomainError(409, "disponibilidad_insuficiente",
                              "No hay disponibilidad suficiente para una de las prendas")


def _devolver_disponibilidad(db, nroSuc, detalles):
    for var_id in sorted({d.idvar for d in detalles}):
        row = comercio.locked_inventario(db, var_id, nroSuc)
        if row is None:
            raise DomainError(409, "inventario_no_encontrado",
                              "No se pudo liberar la disponibilidad de la reserva")
        total = sum(d.cantidad for d in detalles if d.idvar == var_id)
        row.cantdisp = row.cantdisp + total


def _detalle(db, row: Reserva, vencida: bool):
    detalles = repository.detalles(db, row.nroreserva)
    branch = organizacion.branch(db, row.nrosuc)
    city = organizacion.city(db, branch.idciud) if branch else None
    found = repository.variantes_productos(db, sorted({d.idvar for d in detalles}))
    tallas = catalogo.tallas_map(db, list({v.idtalla for v, _ in found.values()}))
    colores = catalogo.variant_colors(db, list(found))
    categorias = {c.idcat: c.descripcion for c in db.scalars(
        select(Categoria).where(Categoria.idcat.in_({p.idcat for _, p in found.values()})))}
    items = []
    for detail in detalles:
        pair = found.get(detail.idvar)
        variant, product = pair if pair else (None, None)
        items.append(ReservaItemDetalle(
            idDetalleRes=detail.iddetalleres, idVar=detail.idvar,
            sku=variant.sku if variant else detail.idvar,
            producto=product.descripcion if product else "Prenda no disponible",
            cantidad=detail.cantidad,
            imagen=variant.img if variant else None,
            talla=tallas[variant.idtalla].descripcion if variant and variant.idtalla in tallas else None,
            categoria=categorias.get(product.idcat) if product else None,
            colores=[c.descripcion for c in colores.get(detail.idvar, [])]))
    return ReservaDetalle(
        nroReserva=row.nroreserva, fechaReserva=row.fechareserva,
        horaAtencion=row.horaatencion, estado=row.estado,
        sucursal=ReservaSucursal(nro=row.nrosuc,
                                 nombre=branch.nombre if branch else str(row.nrosuc),
                                 ciudad=city.nombre if city else ""),
        items=items, totalUnidades=sum(d.cantidad for d in detalles),
        vencida=vencida or row.estado == "vencida")


def liberar_vencidas(db, user_id: str, hoy, peer):
    """Marca reservas vencidas y devuelve su disponibilidad una sola vez.

    PostgreSQL usa el procedimiento oficial. SQLite mantiene una implementación
    equivalente para las pruebas locales, ya que no implementa CALL.
    """
    if is_postgresql(db):
        anteriores = {row.nroreserva for row in
                      repository.reservas_en_estado(db, user_id, "vencida")}
        repository.vencer_con_procedimiento(db)
        db.expire_all()
        actuales = {row.nroreserva for row in
                    repository.reservas_en_estado(db, user_id, "vencida")}
        for _ in actuales - anteriores:
            record(db, "reserva_vencida", user_id, peer, True)
        return actuales

    # Sustituto de CALL para las pruebas SQLite: misma regla y misma cobertura
    # que la PA (pendiente/confirmada con margen sobre fecha + hora de atención),
    # devolviendo la disponibilidad una sola vez.
    liberadas: set[int] = set()
    limite = datetime.now() - timedelta(hours=repository.MARGEN_HORAS)
    for estado in ("pendiente", "confirmada"):
        for row in repository.reservas_en_estado(db, user_id, estado):
            locked = repository.locked_reserva(db, row.nroreserva)
            if locked is None or locked.estado not in {"pendiente", "confirmada"}:
                continue
            if datetime.combine(locked.fechareserva, locked.horaatencion) >= limite:
                continue
            detalles = repository.detalles(db, locked.nroreserva)
            _devolver_disponibilidad(db, locked.nrosuc, detalles)
            locked.estado = "vencida"
            record(db, "reserva_vencida", user_id, peer, True)
            liberadas.add(locked.nroreserva)
    return liberadas


def crear(db, data: ReservaCrear, user_id: str, peer):
    hoy = date.today()
    if data.fechaReserva < hoy:
        raise DomainError(422, "datos_invalidos", "Los datos enviados no son válidos")
    merged = _combinar_items(data.items)
    with transaction(db):
        branch = _sucursal(db, data.nroSuc)
        _validar_horario(db, branch.nro, data.horaAtencion, data.fechaReserva)
        _validar_variantes(db, merged)
        if is_postgresql(db):
            var_ids = sorted(merged)
            nro_reserva = repository.crear_con_procedimiento(
                db, user_id=user_id, nro_suc=branch.nro,
                fecha=data.fechaReserva, hora=data.horaAtencion,
                id_vars=var_ids, cantidades=[merged[var] for var in var_ids])
            db.expire_all()
            row = repository.locked_reserva(db, nro_reserva)
            if row is None:
                raise DomainError(503, "reserva_no_disponible",
                                  "No se pudo completar la reserva")
        else:
            _reservar_disponibilidad(db, branch.nro, merged)
            row = repository.add_reserva(db, Reserva(
                fechareserva=data.fechaReserva, horaatencion=data.horaAtencion,
                estado="pendiente", nrosuc=branch.nro, idusuariocl=user_id))
            for position, var_id in enumerate(sorted(merged), start=1):
                repository.add_detalle(db, DetalleReserva(
                    nroreserva=row.nroreserva, iddetalleres=position,
                    cantidad=merged[var_id], idvar=var_id))
        record(db, "reserva_creada", user_id, peer, True)
        result = _detalle(db, row, vencida=False)
    return result


def listar(db, filters: ReservasFiltros, user_id: str, peer):
    with transaction(db):
        liberadas = liberar_vencidas(db, user_id, date.today(), peer)
        rows, total = repository.list_reservas(db, user_id, filters.estado,
                                              filters.offset, filters.limit)
        items = [_detalle(db, row, vencida=row.nroreserva in liberadas) for row in rows]
    return ReservasListado(items=items, total=total, offset=filters.offset, limit=filters.limit)


def detalle(db, nroReserva: int, user_id: str, peer):
    with transaction(db):
        liberadas = liberar_vencidas(db, user_id, date.today(), peer)
        row = repository.locked_reserva(db, nroReserva)
        if row is None or row.idusuariocl != user_id:
            raise DomainError(404, "reserva_no_encontrada", "Reserva no encontrada")
        return _detalle(db, row, vencida=row.nroreserva in liberadas)


def cancelar(db, nroReserva: int, user_id: str, peer):
    with transaction(db):
        liberadas = liberar_vencidas(db, user_id, date.today(), peer)
        row = repository.locked_reserva(db, nroReserva)
        if row is None or row.idusuariocl != user_id:
            raise DomainError(404, "reserva_no_encontrada", "Reserva no encontrada")
        if row.nroreserva in liberadas or row.estado in ("cancelada", "vencida"):
            # Idempotente: una cancelación repetida (o ya vencida) no devuelve dos veces.
            return _detalle(db, row, vencida=row.nroreserva in liberadas)
        if row.estado != "pendiente":
            raise DomainError(409, "reserva_no_cancelable",
                              "La reserva ya no puede cancelarse")
        if is_postgresql(db):
            repository.cancelar_con_procedimiento(db, row.nroreserva)
            db.expire_all()
            row = repository.locked_reserva(db, nroReserva)
            if row is None:
                raise DomainError(503, "reserva_no_disponible",
                                  "No se pudo cancelar la reserva")
        else:
            detalles = repository.detalles(db, row.nroreserva)
            _devolver_disponibilidad(db, row.nrosuc, detalles)
            row.estado = "cancelada"
        record(db, "reserva_cancelada", user_id, peer, True)
        return _detalle(db, row, vencida=False)


def sucursales_cliente(db):
    rows = organizacion.branches(db, None)
    items = [SucursalCliente(nro=branch.nro, nombre=branch.nombre,
                             direccion=branch.direccion, ciudad=city.nombre)
             for branch, city in rows if branch.estado == "activo"]
    return SucursalesClienteListado(items=items, total=len(items))


def horarios_cliente(db, nroSuc: int):
    branch = organizacion.branch(db, nroSuc)
    if branch is None or branch.estado != "activo":
        raise DomainError(404, "sucursal_no_encontrada", "Sucursal no encontrada")
    rangos = comercio.horarios_sucursal(db, nroSuc)
    return HorariosSucursal(nroSuc=nroSuc, rangos=[
        HorarioRango(horaIni=r.horaini, horaFin=r.horafin, dias=leer_dias(r.dias)) for r in rangos])
