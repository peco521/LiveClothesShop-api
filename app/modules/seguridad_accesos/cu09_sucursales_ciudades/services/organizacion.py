from contextlib import contextmanager

from sqlalchemy.exc import IntegrityError
from sqlalchemy import delete, func, select, text
from app.modules.cliente_experiencia_compra.shared.models.comercio import HorarioAtencion, HorarioSuc
from app.modules.cliente_experiencia_compra.shared.horarios import guardar_dias, leer_dias

from app.core.errors import DomainError
from app.integrations.geocoding import AddressNotFound
from app.modules.seguridad_accesos.shared.models import Ciudad, Sucursal
from app.modules.seguridad_accesos.shared.repositories import continuidad
from app.modules.seguridad_accesos.cu09_sucursales_ciudades.repositories import organizacion as repository
from app.modules.seguridad_accesos.services.bitacora import record
from app.modules.seguridad_accesos.cu09_sucursales_ciudades.schemas.organizacion import (
    CiudadDetalle, CiudadesListado, SucursalDetalle, SucursalesListado,
)


@contextmanager
def transaction(db):
    # Authentication already opened this transaction. Data and audit share it.
    try:
        yield
        db.commit()
    except IntegrityError:
        db.rollback()
        raise DomainError(409, "conflicto_integridad", "Los datos entran en conflicto con un registro existente") from None
    except Exception:
        db.rollback()
        raise


def revalidate_actor(db, actor_id):
    # Fresh reads after target/reference lock waits, without CU06's global lock.
    actor = continuidad.actor(db, actor_id)
    if actor is None:
        raise DomainError(401, "autenticacion_rechazada", "No se pudo autenticar la solicitud")
    if not continuidad.has_permission(db, actor.nrorol, "CU09"):
        raise DomainError(403, "acceso_denegado", "No tiene autorización para esta operación")


def city(db, city_id, *, lock=False, reference=False):
    row = repository.city(db, city_id, lock=lock)
    if row is None:
        raise DomainError(422 if reference else 404, "ciudad_no_encontrada", "Ciudad no encontrada")
    return row


def branch(db, branch_id, *, lock=False):
    row = repository.branch(db, branch_id, lock=lock)
    if row is None:
        raise DomainError(404, "sucursal_no_encontrada", "Sucursal no encontrada")
    return row


def city_detail(row):
    return CiudadDetalle(id=row.id, nombre=row.nombre)


def address_coordinates(direccion, city_row, geocoder):
    """CU09: verifica que la dirección exista como ubicación real.

    Devuelve (latitud, longitud) o (None, None) si la validación está
    desactivada. Un fallo del proveedor no se confunde con una dirección
    inexistente y nunca se guardan coordenadas parciales.
    """
    if geocoder is None:
        return None, None
    try:
        return geocoder.locate(direccion, city_row.nombre)
    except AddressNotFound:
        raise DomainError(422, "direccion_no_verificada",
                          "La dirección no corresponde a una ubicación conocida en esa ciudad") from None
    except Exception:
        raise DomainError(503, "geocodificacion_no_disponible",
                          "No se pudo validar la dirección en este momento; intente nuevamente") from None


def branch_detail(row, city_row, db):
    horarios = db.execute(select(HorarioAtencion, HorarioSuc.dias).join(HorarioSuc, HorarioSuc.idaten == HorarioAtencion.idaten)
                          .where(HorarioSuc.nrosuc == row.nro).order_by(HorarioAtencion.horaini, HorarioAtencion.horafin)).all()
    return SucursalDetalle(nro=row.nro, nombre=row.nombre, direccion=row.direccion,
                           estado=row.estado, idCiud=row.idciud, ciudad=city_detail(city_row),
                           latitud=row.latitud, longitud=row.longitud,
                           horarios=[{"horaIni": h.horaini, "horaFin": h.horafin, "dias": leer_dias(dias)} for h, dias in horarios])


def replace_hours(db, branch_id, ranges):
    # Los rangos pueden compartirse: cambiar únicamente los enlaces de esta sucursal.
    # La BD original usa un SMALLINT manual. El bloqueo serializa la asignación
    # incluso con otras conexiones que insertan horarios, sin cambiar el esquema.
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("LOCK TABLE horario_atencion IN SHARE ROW EXCLUSIVE MODE"))
    ids = []
    for hours in ranges:
        start = hours.horaIni if hasattr(hours, "horaIni") else hours["horaIni"]
        end = hours.horaFin if hasattr(hours, "horaFin") else hours["horaFin"]
        row = db.scalar(select(HorarioAtencion).where(HorarioAtencion.horaini == start, HorarioAtencion.horafin == end)
                        .order_by(HorarioAtencion.idaten).limit(1))
        if row is None:
            next_id = max(0, db.scalar(select(func.max(HorarioAtencion.idaten))) or 0) + 1
            if next_id > 32767:
                raise DomainError(409, "horarios_agotados", "Se alcanzó el límite de horarios registrados")
            row = HorarioAtencion(idaten=next_id, horaini=start, horafin=end)
            db.add(row)
            db.flush()
        days = hours.dias if hasattr(hours, "dias") else hours["dias"]
        ids.append((row.idaten, guardar_dias(days)))
    db.execute(delete(HorarioSuc).where(HorarioSuc.nrosuc == branch_id))
    db.add_all([HorarioSuc(nrosuc=branch_id, idaten=idaten, dias=dias) for idaten, dias in ids])
    db.flush()


def detail_branch(db, branch_id):
    row = branch(db, branch_id)
    return branch_detail(row, city(db, row.idciud), db)


def list_cities(db, filters):
    rows, total = repository.list_cities(db, filters)
    return CiudadesListado(items=[city_detail(row) for row in rows], total=total,
                           offset=filters.offset, limit=filters.limit)


def list_branches(db, filters):
    rows, total = repository.list_branches(db, filters)
    return SucursalesListado(items=[branch_detail(row, city_row, db) for row, city_row in rows],
                             total=total, offset=filters.offset, limit=filters.limit)


def create_city(db, data, actor_id, peer):
    with transaction(db):
        revalidate_actor(db, actor_id)
        row = Ciudad(nombre=data.nombre)
        repository.add(db, row)
        record(db, "ciudad_creada", actor_id, peer, True)
        result = city_detail(row)
    return result


def edit_city(db, city_id, data, actor_id, peer):
    with transaction(db):
        row = city(db, city_id, lock=True)
        revalidate_actor(db, actor_id)
        if row.nombre != data.nombre:
            row.nombre = data.nombre
            record(db, "ciudad_actualizada", actor_id, peer, True)
        result = city_detail(row)
    return result


def create_branch(db, data, actor_id, peer, geocoder=None):
    with transaction(db):
        city_row = city(db, data.idCiud, lock=True, reference=True)
        revalidate_actor(db, actor_id)
        latitud, longitud = address_coordinates(data.direccion, city_row, geocoder)
        row = Sucursal(nombre=data.nombre, direccion=data.direccion, estado=data.estado,
                       idciud=data.idCiud, latitud=latitud, longitud=longitud)
        repository.add(db, row)
        if data.horarios:
            replace_hours(db, row.nro, data.horarios)
            revalidate_actor(db, actor_id)
        record(db, "sucursal_creada", actor_id, peer, True)
        result = branch_detail(row, city_row, db)
    return result


def edit_branch(db, branch_id, data, actor_id, peer, geocoder=None):
    with transaction(db):
        row = branch(db, branch_id, lock=True)
        changes = data.model_dump(exclude_unset=True)
        hours = changes.pop("horarios", None)
        city_row = city(db, changes.get("idCiud", row.idciud), lock=True, reference=True)
        revalidate_actor(db, actor_id)
        columns = {"nombre": "nombre", "direccion": "direccion", "estado": "estado", "idCiud": "idciud"}
        changes = {key: value for key, value in changes.items() if value != getattr(row, columns[key])}
        for key, value in changes.items():
            setattr(row, columns[key], value)
        if {"direccion", "idCiud"} & changes.keys():
            # Cambió la ubicación declarada: se vuelve a verificar contra el proveedor.
            row.latitud, row.longitud = address_coordinates(row.direccion, city_row, geocoder)
        hours_changed = hours is not None and sorted(hours, key=lambda h: (h["horaIni"], h["horaFin"])) != [
            h.model_dump() for h in branch_detail(row, city_row, db).horarios]
        if hours_changed:
            replace_hours(db, row.nro, hours)
            revalidate_actor(db, actor_id)
        if changes.keys() - {"estado"} or hours_changed:
            record(db, "sucursal_actualizada", actor_id, peer, True)
        if "estado" in changes:
            record(db, "sucursal_estado_actualizado", actor_id, peer, True)
        result = branch_detail(row, city_row, db)
    return result
