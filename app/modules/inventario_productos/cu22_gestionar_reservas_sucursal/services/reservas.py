from datetime import date
from sqlalchemy import func, select
from app.core.errors import DomainError
from app.modules.cliente_experiencia_compra.shared.models.comercio import Reserva, Venta
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


def expire(db, scope_id=None):
    with transaction(db):
        query = select(Reserva).where(Reserva.estado.in_(['pendiente', 'confirmada']), Reserva.fechareserva < date.today()).order_by(Reserva.nroreserva).with_for_update()
        if scope_id is not None:
            query = query.where(Reserva.nrosuc == scope_id)
        for row in db.scalars(query):
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
        if db.scalar(select(Venta.nroventa).where(Venta.nroreserva == nro, Venta.estado == 'registrada')):
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
