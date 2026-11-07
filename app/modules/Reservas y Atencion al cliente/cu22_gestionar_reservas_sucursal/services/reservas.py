from datetime import date, datetime, timedelta
from sqlalchemy import func, select
from app.core.database import is_postgresql
from app.core.errors import DomainError
from app.modules.cliente_experiencia_compra.shared.models.comercio import Pago, Reserva, Venta
from app.modules.cliente_experiencia_compra.shared.repositories import comercio
from app.modules.cliente_experiencia_compra.cu11_gestionar_reserva.repositories import reserva as repo
from app.modules.cliente_experiencia_compra.cu11_gestionar_reserva.services import reserva as client
from app.modules.inventario_productos.shared.operaciones_access import branch
from app.modules.inventario_productos.shared.access import transaction
from app.modules.seguridad_accesos.services.bitacora import record


def release(db, row):
    for detail in sorted(repo.detalles(db, row.nroreserva), key=lambda d: d.idvar):
        rows = comercio.locked_inventarios(db, detail.idvar, row.nrosuc)
        if sum(r.stock - r.cantdisp for r in rows) < detail.cantidad:
            raise DomainError(409, 'inventario_inconsistente', 'Las existencias reservadas necesitan revisión')
        left = detail.cantidad
        for inventory in rows:
            amount = min(left, inventory.stock - inventory.cantdisp)
            inventory.cantdisp += amount
            left -= amount


MARGEN_HORAS = repo.MARGEN_HORAS

# CU22 → CU24: en caja sólo se cobran reservas con las prendas ya preparadas.
ESTADOS_COBRO = ('confirmada', 'atendida')


def expire(db, scope_id=None):
    """CU22: vence reservas vencidas usando fecha + hora de atención + 3 horas.

    En PostgreSQL la fuente de verdad es la PA `sp_marcar_reservas_vencidas`:
    ella marca 'vencida' y libera la disponibilidad retenida una sola vez, así
    que Python NO repite el proceso. El sustituto SQLite reproduce exactamente
    la misma regla para las pruebas. No se vence una reserva sólo porque su
    fecha sea anterior a hoy.
    """
    with transaction(db):
        if is_postgresql(db):
            # La PA es la fuente de verdad: marca 'vencida' y libera en SQL una sola vez.
            repo.vencer_con_procedimiento(db, MARGEN_HORAS)
            return
        limite = datetime.now() - timedelta(hours=MARGEN_HORAS)
        query = select(Reserva).where(Reserva.estado.in_(['pendiente', 'confirmada'])
                                      ).order_by(Reserva.nroreserva).with_for_update()
        if scope_id is not None:
            query = query.where(Reserva.nrosuc == scope_id)
        for row in db.scalars(query):
            if datetime.combine(row.fechareserva, row.horaatencion) >= limite:
                continue  # Todavía vigente dentro del margen de atención.
            release(db, row)
            row.estado = 'vencida'
            record(db, 'reserva_vencida', None, None, True)


def get(db, nro, scope_id, lock=False):
    query = select(Reserva).where(Reserva.nroreserva == nro)
    if scope_id is not None:
        query = query.where(Reserva.nrosuc == scope_id)
    row = db.scalar(query.with_for_update() if lock else query)
    if row is None:
        raise DomainError(404, 'reserva_no_encontrada', 'Reserva no encontrada en tu sucursal')
    return row


def detalle(db, row):
    """Detalle de reserva para el panel de sucursal (incluye el cliente de la reserva)."""
    value = client._detalle(db, row, False).model_dump()
    value['idCliente'] = row.idusuariocl
    return value


def venta_vinculada(db, nro):
    """Venta de caja vinculada a la reserva (la última que no fue anulada)."""
    return db.scalar(select(Venta).where(Venta.nroreserva == nro, Venta.estado != 'anulada')
                     .order_by(Venta.nroventa.desc()))


def estado_cobro(db, nro):
    """(venta pendiente de cobro, venta ya cobrada) de una reserva.

    Una venta de caja permanece en ``registrada`` se haya cobrado o no: el cobro
    real vive en ``pago.estado``. Por eso se distingue la venta **preparada y
    todavía impaga** --la caja la retoma, no bloquea la reserva-- de la venta **ya
    aprobada**, que sí significa que la reserva fue vendida. Una venta ``anulada``
    se ignora: la reserva vuelve a estar disponible para cobrarse.
    """
    venta = venta_vinculada(db, nro)
    if venta is None:
        return None, None
    aprobado = db.scalar(select(Pago.idpago).where(Pago.nroventa == venta.nroventa,
                                                   Pago.estado == 'aprobado'))
    return (None, venta) if aprobado is not None else (venta, None)


def cobro(db, nro, scope_id):
    """Reserva lista para cobrarse en caja (la usa CU24, que no tiene permiso de CU22).

    Reutiliza la misma lectura del panel de sucursal —prendas, cantidades, sucursal
    y cliente titular— para que la venta se reconstruya desde la reserva y no desde
    datos temporales enviados por el navegador. No libera disponibilidad ni cambia
    el estado: eso ocurre únicamente al aprobarse el pago.

    Si la caja ya preparó la venta de esta reserva, la respuesta la informa en
    ``nroVentaEnCurso`` para retomar esa misma venta: una venta preparada y sin
    pagar no significa que la reserva esté vendida. Sólo una venta ya cobrada
    bloquea la reserva.
    """
    expire(db, scope_id)
    row = get(db, nro, scope_id)
    if row.estado not in ESTADOS_COBRO:
        raise DomainError(409, 'reserva_no_preparada',
                          'Confirma las prendas preparadas de la reserva antes de cobrarla en caja')
    pendiente, cobrada = estado_cobro(db, nro)
    if cobrada is not None:
        raise DomainError(409, 'reserva_vendida', 'Esta reserva ya fue cobrada en una venta')
    value = detalle(db, row)
    value['nroVentaEnCurso'] = pendiente.nroventa if pendiente is not None else None
    return value


def listing(db, scope_id, estado, offset, limit):
    expire(db, scope_id)
    conditions = [Reserva.estado == estado] if estado else [Reserva.estado.in_(['pendiente', 'confirmada'])]
    if scope_id is not None:
        conditions.append(Reserva.nrosuc == scope_id)
    rows = db.scalars(select(Reserva).where(*conditions).order_by(Reserva.fechareserva, Reserva.horaatencion, Reserva.nroreserva).offset(offset).limit(limit))
    return dict(items=[detalle(db, r) for r in rows], total=db.scalar(select(func.count()).select_from(Reserva).where(*conditions)), offset=offset, limit=limit)


def update(db, nro, data, actor, scope_id, peer):
    with transaction(db):
        row = get(db, nro, scope_id, True)
        if row.estado not in {'pendiente', 'confirmada'} or row.fechareserva < date.today():
            raise DomainError(409, 'reserva_no_gestionable', 'La reserva fue cancelada, atendida o venció; vuelve a consultar')
        if venta_vinculada(db, nro) is not None:
            raise DomainError(409, 'venta_en_curso', 'La reserva está vinculada a una venta; finaliza o cancela esa venta primero')
        if data.accion == 'reprogramar':
            if data.fechaReserva is None or data.horaAtencion is None or data.fechaReserva < date.today():
                raise DomainError(422, 'datos_invalidos', 'Indica una fecha futura y una hora válidas')
            client._validar_horario(db, row.nrosuc, data.horaAtencion, data.fechaReserva)
            row.fechareserva, row.horaatencion = data.fechaReserva, data.horaAtencion
        else:
            for detail in repo.detalles(db, nro):
                rows = comercio.locked_inventarios(db, detail.idvar, row.nrosuc)
                if sum(r.stock for r in rows) < detail.cantidad:
                    raise DomainError(409, 'inventario_inconsistente', 'La prenda reservada ya no está físicamente disponible')
            if data.accion == 'confirmar':
                row.estado = 'confirmada'
            else:
                if row.estado != 'confirmada':
                    raise DomainError(409, 'reserva_no_confirmada', 'Confirma la preparación de las prendas antes de atender')
                release(db, row)
                row.estado = 'atendida'
        record(db, 'reserva_' + data.accion, actor, peer, True)
        db.flush()
        return detalle(db, row)
