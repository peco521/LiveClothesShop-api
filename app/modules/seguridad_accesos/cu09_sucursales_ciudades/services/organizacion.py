from contextlib import contextmanager

from sqlalchemy.exc import IntegrityError

from app.core.errors import DomainError
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
    if actor is None or not actor.activo:
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


def branch_detail(row, city_row):
    return SucursalDetalle(nro=row.nro, nombre=row.nombre, direccion=row.direccion,
                           estado=row.estado, idCiud=row.idciud, ciudad=city_detail(city_row))


def detail_branch(db, branch_id):
    row = branch(db, branch_id)
    return branch_detail(row, city(db, row.idciud))


def list_cities(db, filters):
    rows, total = repository.list_cities(db, filters)
    return CiudadesListado(items=[city_detail(row) for row in rows], total=total,
                           offset=filters.offset, limit=filters.limit)


def list_branches(db, filters):
    rows, total = repository.list_branches(db, filters)
    return SucursalesListado(items=[branch_detail(row, city_row) for row, city_row in rows],
                             total=total, offset=filters.offset, limit=filters.limit)


def create_city(db, data, actor_id, peer):
    with transaction(db):
        revalidate_actor(db, actor_id)
        if repository.city(db, data.id) is not None:
            raise DomainError(409, "ciudad_duplicada", "El identificador de ciudad ya existe")
        row = Ciudad(id=data.id, nombre=data.nombre)
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


def create_branch(db, data, actor_id, peer):
    with transaction(db):
        city_row = city(db, data.idCiud, lock=True, reference=True)
        revalidate_actor(db, actor_id)
        row = Sucursal(nombre=data.nombre, direccion=data.direccion, estado=data.estado, idciud=data.idCiud)
        repository.add(db, row)
        record(db, "sucursal_creada", actor_id, peer, True)
        result = branch_detail(row, city_row)
    return result


def edit_branch(db, branch_id, data, actor_id, peer):
    with transaction(db):
        row = branch(db, branch_id, lock=True)
        changes = data.model_dump(exclude_unset=True)
        city_row = city(db, changes.get("idCiud", row.idciud), lock=True, reference=True)
        revalidate_actor(db, actor_id)
        columns = {"nombre": "nombre", "direccion": "direccion", "estado": "estado", "idCiud": "idciud"}
        changes = {key: value for key, value in changes.items() if value != getattr(row, columns[key])}
        for key, value in changes.items():
            setattr(row, columns[key], value)
        if changes.keys() - {"estado"}:
            record(db, "sucursal_actualizada", actor_id, peer, True)
        if "estado" in changes:
            record(db, "sucursal_estado_actualizado", actor_id, peer, True)
        result = branch_detail(row, city_row)
    return result
