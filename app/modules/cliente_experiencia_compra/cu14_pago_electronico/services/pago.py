"""CU14 Pago electrónico: venta registrada → cobro mock → cierre transaccional.

Fases separadas: la llamada a la pasarela NUNCA ocurre con transacción
abierta. El "aprobado" del mock es autorización de cobro; la aprobación
FINAL (con descuento de stock) solo se persiste si el stock se confirma
bajo lock en la fase 3. Así nunca existe pago aprobado sin entregable.
"""

from contextlib import contextmanager
from datetime import date
from decimal import Decimal

from sqlalchemy.exc import IntegrityError

from app.core.errors import DomainError
from app.integrations.payments.protocolo import ResultadoPasarela, TimeoutPasarela
from app.modules.cliente_experiencia_compra.cu12_carrito.repositories import carrito as carrito_repo
from app.modules.cliente_experiencia_compra.cu13_compra_digital.repositories import venta as venta_repo
from app.modules.cliente_experiencia_compra.cu14_pago_electronico.repositories import pago as repository
from app.modules.cliente_experiencia_compra.cu14_pago_electronico.schemas.pago import (
    PagoCrear,
    PagoDetalle,
    PagoReprocesar,
)
from app.modules.cliente_experiencia_compra.shared.models.comercio import MovimientoInv, Pago
from app.modules.cliente_experiencia_compra.shared.repositories import comercio as comercio_repo
from app.modules.seguridad_accesos.services.bitacora import record


@contextmanager
def transaction(db):
    # require_cliente ya abrió esta transacción en la sesión compartida.
    try:
        yield
        db.commit()
    except IntegrityError:
        db.rollback()
        raise DomainError(409, "conflicto_integridad", "Los datos entran en conflicto con un registro existente") from None
    except Exception:
        db.rollback()
        raise


def _vista(db, pago: Pago):
    venta = repository.venta_de_pago(db, pago)
    return PagoDetalle(idPago=pago.idpago, metodo=pago.metodo, monto=pago.monto,
                       estado=pago.estado, fechaHora=pago.fechahora,
                       referencia=pago.referencia, nroVenta=pago.nroventa,
                       estadoVenta=venta.estado if venta else "")


def _validar_escenario(escenario, environment: str):
    # Sandbox académico: el escenario solo existe fuera de producción.
    if escenario is not None and environment == "production":
        raise DomainError(422, "datos_invalidos", "Los datos enviados no son válidos")


def iniciar(db, data: PagoCrear, user_id: str, peer, pasarela, environment: str):
    """Fase 1 (commit) + fase 2 (pasarela, sin tx) + fase 3 (commit)."""
    _validar_escenario(data.escenario, environment)
    with transaction(db):
        carrito_repo.locked_cliente(db, user_id)
        venta = repository.locked_venta(db, data.nroVenta)
        if venta is None or venta.idusuariocl != user_id:
            raise DomainError(404, "venta_no_encontrada", "Venta no encontrada")
        if venta.estado != "registrada":
            raise DomainError(409, "venta_no_pagable", "La venta ya no puede pagarse")
        for previo in repository.pagos_de_venta(db, venta.nroventa):
            if previo.estado == "aprobado":
                return _vista(db, previo), False  # Idempotente: ya cobrado.
            if previo.estado == "pendiente":
                return _vista(db, previo), False  # Sin duplicar pendientes.
        pago = repository.add_pago(db, Pago(
            metodo=data.metodo, monto=venta.total, estado="pendiente",
            referencia=None, nroventa=venta.nroventa))
        record(db, "pago_iniciado", user_id, peer, True)
        snapshot = (pago.idpago, venta.total, data.metodo, venta.nroventa, data.escenario)
    resultado = _cobrar(pasarela, snapshot)
    with transaction(db):
        return _resolver_mock(db, snapshot[0], user_id, peer, resultado)


def consultar(db, idPago: int, user_id: str):
    pago = repository.pago_propio(db, idPago, user_id)
    if pago is None:
        raise DomainError(404, "pago_no_encontrado", "Pago no encontrado")
    return _vista(db, pago)


def reprocesar(db, idPago: int, data: PagoReprocesar, user_id: str, peer, pasarela, environment: str):
    """Reintento controlado de un pago pendiente (p. ej. tras timeout)."""
    _validar_escenario(data.escenario, environment)
    with transaction(db):
        carrito_repo.locked_cliente(db, user_id)
        pago = repository.locked_pago(db, idPago)
        venta = repository.venta_de_pago(db, pago) if pago else None
        if pago is None or venta is None or venta.idusuariocl != user_id:
            raise DomainError(404, "pago_no_encontrado", "Pago no encontrado")
        if pago.estado != "pendiente":
            return _vista(db, pago), False  # Terminal: no se reintenta.
        snapshot = (pago.idpago, pago.monto, pago.metodo, venta.nroventa, data.escenario)
    resultado = _cobrar(pasarela, snapshot)
    with transaction(db):
        return _resolver_mock(db, snapshot[0], user_id, peer, resultado)


def _resolver_mock(db, id_pago, user_id, peer, resultado):
    try:
        with db.begin_nested():
            return _finalizar(db, id_pago, user_id, peer, resultado)
    except DomainError as error:
        if error.code not in {"disponibilidad_insuficiente", "carrito_modificado", "carrito_no_disponible"}:
            raise
        # The mock has not charged real money. Resolve the failed delivery
        # atomically rather than leave a permanently "approved but pending" job.
        return _finalizar(db, id_pago, user_id, peer, ResultadoPasarela("rechazado"))


def _cobrar(pasarela, snapshot):
    id_pago, monto, metodo, nro_venta, escenario = snapshot
    try:
        return pasarela.cobrar(monto=monto, metodo=metodo, nro_venta=nro_venta,
                               id_pago=id_pago, escenario=escenario)
    except TimeoutPasarela:
        return None  # Resultado desconocido: el pago queda pendiente.
    except Exception:
        return None  # Error de comunicación: pendiente, reconciliable.


def _finalizar(db, id_pago: int, user_id: str, peer, resultado: ResultadoPasarela | None):
    carrito_repo.locked_cliente(db, user_id)
    pago = repository.locked_pago(db, id_pago)
    venta = repository.venta_de_pago(db, pago) if pago else None
    if pago is None or venta is None or venta.idusuariocl != user_id:
        raise DomainError(404, "pago_no_encontrado", "Pago no encontrado")
    if pago.estado != "pendiente" or venta.estado != "registrada":
        # Doble aprobación/callback: ya resuelto, sin tocar inventario.
        return _vista(db, pago), False
    if resultado is None:
        # Timeout/error sin certeza: el pago queda pendiente, reconciliable.
        # No se convierte a rechazado automáticamente.
        return _vista(db, pago), True
    if resultado.estado == "rechazado":
        pago.estado = "rechazado"
        pago.referencia = resultado.referencia if resultado else None
        venta.estado = "anulada"
        record(db, "pago_rechazado", user_id, peer, True)
        record(db, "venta_anulada", user_id, peer, True)
        return _vista(db, pago), True
    # Autorizado por la pasarela: confirmar stock ANTES de persistir aprobado.
    _descontar(db, venta, user_id, peer)
    pago.estado = "aprobado"
    pago.referencia = resultado.referencia
    record(db, "pago_aprobado", user_id, peer, True)
    return _vista(db, pago), True


def _descontar(db, venta, user_id: str, peer):
    cart = carrito_repo.locked_active_cart(db, user_id)
    if cart is None or cart.idcarrito != venta.idcarrito:
        # El carrito ya no está activo para esta venta: conflicto, sin descuento.
        raise DomainError(409, "carrito_no_disponible",
                          "El carrito de esta venta ya no está activo")
    detalles = venta_repo.detalles(db, venta.nroventa)
    actual = sorted((d.idvar, d.cantidad) for d in carrito_repo.cart_details(db, cart.idcarrito))
    if actual != sorted((d.idvar, d.cantidad) for d in detalles):
        raise DomainError(409, "carrito_modificado", "El carrito cambió desde la preparación de la compra")
    for detail in sorted(detalles, key=lambda d: d.idvar):
        filas = comercio_repo.locked_inventarios(db, detail.idvar, venta.nrosuc)
        if sum(r.cantdisp for r in filas) < detail.cantidad \
                or sum(r.stock for r in filas) < detail.cantidad:
            raise DomainError(409, "disponibilidad_insuficiente",
                              "No hay disponibilidad suficiente en la sucursal")
    for detail in sorted(detalles, key=lambda d: d.idvar):
        pendiente = detail.cantidad
        for row in comercio_repo.locked_inventarios(db, detail.idvar, venta.nrosuc):
            if pendiente <= 0:
                break
            toma = min(pendiente, row.cantdisp, row.stock)
            if toma <= 0:
                continue
            row.cantdisp -= toma
            row.stock -= toma
            repository.add_movimiento(db, MovimientoInv(
                tipomov="salida", cantidad=toma, fecha=date.today(),
                motivo=f"venta digital nro {venta.nroventa}", nroinv=row.nroinv))
            pendiente -= toma
        if pendiente > 0:  # Defensa en profundidad (el chequeo previo lo impide).
            raise DomainError(409, "disponibilidad_insuficiente",
                              "No hay disponibilidad suficiente en la sucursal")
    cart.estado = "convertido"
