from datetime import date
from decimal import Decimal
from sqlalchemy import or_, select
from app.core.errors import DomainError
from app.modules.inventario_productos.shared.operaciones_access import branch
from app.modules.inventario_productos.shared.access import transaction
from app.modules.seguridad_accesos.models import Usuario, Cliente
from app.modules.seguridad_accesos.services.bitacora import record
from app.modules.cliente_experiencia_compra.shared.models.comercio import Venta, DetalleVenta, Pago, Reserva
from app.modules.cliente_experiencia_compra.shared.repositories import comercio, catalogo
from app.modules.cliente_experiencia_compra.shared.services.precios import moneda, descuento_unitario
from app.modules.cliente_experiencia_compra.cu11_gestionar_reserva.repositories import reserva as variants
from app.modules.cliente_experiencia_compra.cu13_compra_digital.services.compra import _vista
from app.modules.cliente_experiencia_compra.cu14_pago_electronico.services import pago as payments, checkout_stripe
from app.modules.cliente_experiencia_compra.cu14_pago_electronico.schemas.pago import PagoCrear
from app.modules.seguridad_accesos.shared.models import Sucursal


def get(db, nro, scope_id, lock=False):
    query = select(Venta).where(Venta.nroventa == nro, Venta.claveoperacion.is_not(None))
    if scope_id is not None:
        query = query.where(Venta.nrosuc == scope_id)
    row = db.scalar(query.with_for_update() if lock else query)
    if not row:
        raise DomainError(404, 'venta_no_encontrada', 'Venta de caja no encontrada en tu sucursal')
    return row


def view(db, row):
    value = _vista(db, row).model_dump()
    value['pagos'] = [payments._vista(db, p) for p in db.scalars(select(Pago).where(Pago.nroventa == row.nroventa).order_by(Pago.idpago))]
    return value


def customers(db, q, id_usuario=None):
    conditions = [Usuario.tipo == 'C', Cliente.estado != 'inactivo']
    if id_usuario:
        # Búsqueda directa del cliente que llegó con una reserva (CU22 → CU24).
        conditions.append(Usuario.idusuario == id_usuario)
    else:
        conditions.append(or_(Usuario.nombre.ilike('%' + q + '%'), Usuario.correo.ilike('%' + q + '%'),
                              Usuario.ci.ilike('%' + q + '%')))
    users = db.scalars(select(Usuario).join(Cliente, Cliente.idusuario == Usuario.idusuario)
                       .where(*conditions).order_by(Usuario.nombre).limit(20))
    return [dict(idUsuario=u.idusuario, nombre=' '.join([u.nombre, u.apellidopat, u.apellidomat]), correo=u.correo, ci=u.ci) for u in users]


def create(db, data, actor, scope_id, peer):
    branch(scope_id, data.nroSuc)
    with transaction(db):
        user = db.scalar(select(Usuario).where(Usuario.idusuario == actor).with_for_update())
        old = db.scalar(select(Venta).where(Venta.claveoperacion == str(data.claveOperacion)))
        if old:
            branch(scope_id, old.nrosuc)
            if user.tipo != 'A' and old.idusuarioemp != actor:
                raise DomainError(409, 'operacion_duplicada', 'La operación pertenece a otra sesión de caja')
            return view(db, old)
        if db.get(Cliente, data.idCliente) is None:
            raise DomainError(404, 'cliente_no_encontrado', 'Selecciona un cliente registrado')
        store = db.get(Sucursal, data.nroSuc)
        if store is None or store.estado != 'activo':
            raise DomainError(404, 'sucursal_no_encontrada', 'Sucursal no disponible')
        if data.nroReserva:
            row = db.scalar(select(Reserva).where(Reserva.nroreserva == data.nroReserva).with_for_update())
            if not row or row.idusuariocl != data.idCliente or row.nrosuc != data.nroSuc:
                raise DomainError(404, 'reserva_no_encontrada', 'La reserva no corresponde a este cliente y sucursal')
            if row.estado not in {'confirmada', 'atendida'}:
                raise DomainError(409, 'reserva_no_preparada', 'Confirma las prendas preparadas de la reserva antes de cobrarla en caja')
            if db.scalar(select(Venta.nroventa).where(Venta.nroreserva == row.nroreserva, Venta.estado == 'registrada')):
                raise DomainError(409, 'reserva_vendida', 'Esta reserva ya está vinculada a una venta')
        quantities = {}
        for item in data.items:
            quantities[item.idVar] = quantities.get(item.idVar, 0) + item.cantidad
        found = variants.variantes_productos(db, sorted(quantities))
        promos = catalogo.promociones(db, [p.idpromo for _, p in found.values()])
        gross = discount = Decimal('0')
        details = []
        for key, quantity in sorted(quantities.items()):
            pair = found.get(key)
            if not pair or any(p.estado != 'activo' for p in pair):
                raise DomainError(404, 'producto_no_encontrado', 'Una prenda ya no está disponible')
            rows = comercio.locked_inventarios(db, key, data.nroSuc)
            if min(sum(r.cantdisp for r in rows), sum(r.stock for r in rows)) < quantity:
                raise DomainError(409, 'disponibilidad_insuficiente', 'No hay unidades suficientes en esta sucursal')
            variant, product = pair
            base = moneda(variant.precio)
            gross += base * quantity
            discount += descuento_unitario(base, promos.get(product.idpromo)) * quantity
            details.append((key, quantity, base, descuento_unitario(base, promos.get(product.idpromo))))
        row = Venta(idusuariocl=data.idCliente, idusuarioemp=actor if user.tipo == 'E' else None, nrosuc=data.nroSuc, nit=data.nit, nroreserva=data.nroReserva, claveoperacion=str(data.claveOperacion), estado='registrada', total=moneda(gross-discount), desc_aplicado=moneda(discount))
        db.add(row); db.flush()
        # fechahora y nroventa los genera la base de datos; se refrescan antes de
        # construir la vista para no serializar un nroVenta/fecha nulos.
        db.refresh(row, attribute_names=['nroventa', 'fechahora'])
        for i, (key, qty, base, disc) in enumerate(details, 1):
            db.add(DetalleVenta(nroventa=row.nroventa, iddetalleventa=i, idvar=key, cantidad=qty, preciounitario=base, descuentounitario=disc))
        db.flush(); record(db, 'venta_caja_preparada', actor, peer, True)
        return view(db, row)


def cash(db, nro, received, actor, scope_id, peer):
    with transaction(db):
        row = get(db, nro, scope_id, True)
        paid = list(db.scalars(select(Pago).where(Pago.nroventa == nro)))
        approved = next((p for p in paid if p.estado == 'aprobado'), None)
        if approved:
            return view(db, row)
        if row.estado != 'registrada' or any(p.estado == 'pendiente' for p in paid):
            raise DomainError(409, 'pago_en_curso', 'La venta fue cancelada o tiene un pago en curso')
        if received < row.total:
            raise DomainError(422, 'efectivo_insuficiente', 'El efectivo recibido no cubre el total')
        payments._descontar(db, row, row.idusuariocl, peer)
        db.add(Pago(nroventa=nro, metodo='efectivo', monto=row.total, estado='aprobado', referencia=f'efectivo:recibido={received}'))
        db.flush(); record(db, 'venta_caja_pagada', actor, peer, True)
        value = view(db, row); value['cambio'] = moneda(received-row.total)
        return value


def electronic(db, nro, metodo, actor, scope_id, peer, app):
    row = get(db, nro, scope_id)
    owner = row.idusuariocl
    db.commit()  # No remote request inside a transaction.
    data = PagoCrear(nroVenta=nro, metodo=metodo)
    if app.state.settings.payments_provider == 'stripe':
        result, _ = checkout_stripe.start(db, data, owner, peer, app.state.stripe)
    else:
        if app.state.settings.environment == 'production':
            raise DomainError(503, 'pasarela_no_configurada', 'Configura una pasarela real antes de cobrar')
        result, _ = payments.iniciar(db, data, owner, peer, app.state.pasarela, app.state.settings.environment)
    with transaction(db):
        record(db, 'venta_caja_pago_electronico', actor, peer, True)
    return result
