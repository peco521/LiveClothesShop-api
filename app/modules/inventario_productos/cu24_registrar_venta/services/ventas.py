import secrets
from datetime import date
from decimal import Decimal
from uuid import uuid4
from sqlalchemy import or_, select
from app.core.errors import DomainError
from app.modules.inventario_productos.shared.operaciones_access import branch
from app.modules.inventario_productos.shared.access import transaction
from app.modules.seguridad_accesos.models import Usuario, Cliente
from app.modules.seguridad_accesos.repositories import rol as roles_repo
from app.modules.seguridad_accesos.repositories import usuario as users_repo
from app.modules.seguridad_accesos.services.bitacora import record
from app.modules.cliente_experiencia_compra.shared.models.comercio import Venta, DetalleVenta, Pago, Reserva
from app.modules.cliente_experiencia_compra.shared.repositories import comercio, catalogo
from app.modules.cliente_experiencia_compra.shared.services.precios import moneda, descuento_unitario
from app.modules.cliente_experiencia_compra.cu11_gestionar_reserva.repositories import reserva as variants
from app.modules.inventario_productos.cu22_gestionar_reservas_sucursal.services import reservas
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


def view(db, row, operator=None):
    """Vista de la venta de caja: cobro y datos que imprime el recibo (CU24).

    ``operator`` es el usuario que atiende la caja. Sólo se usa cuando la venta
    no tiene empleado asignado: ``venta.idUsuarioEmp`` referencia a ``empleado``,
    así que una venta hecha por un administrador queda en NULL. El recibo muestra
    entonces a quien atendió, nunca un nombre inventado ni datos sensibles.
    """
    value = _vista(db, row).model_dump()
    value['pagos'] = [payments._vista(db, p) for p in db.scalars(select(Pago).where(Pago.nroventa == row.nroventa).order_by(Pago.idpago))]
    value['cliente'] = _vista_persona(db, row.idusuariocl, apellido_mat=True)
    value['empleado'] = _vista_persona(db, row.idusuarioemp or operator, apellido_mat=False)
    value['descuentos'] = _vista_descuentos(db, row)
    return value


def _vista_persona(db, user_id, apellido_mat):
    """Nombre de la persona que identifica la venta; None si la venta es anónima."""
    user = db.get(Usuario, user_id) if user_id else None
    if user is None:
        return None
    partes = [user.nombre, user.apellidopat] + ([user.apellidomat] if apellido_mat else [])
    return ' '.join(parte.strip() for parte in partes if parte and parte.strip()) or None


def _vista_descuentos(db, row):
    """Descuentos aplicados a la venta para el ticket.

    El importe NUNCA se recalcula: sale de ``detalleventa.descuentoUnitario`` y el
    producto sólo aporta el nombre, el tipo y el valor de la promoción que lo
    originó. Si esa promoción ya no existe (o el producto dejó de tenerla) se
    informa el importe congelado como descuento aplicado, sin inventar un nombre.
    """
    detalles = list(db.scalars(select(DetalleVenta).where(DetalleVenta.nroventa == row.nroventa)))
    con_descuento = [d for d in detalles if d.descuentounitario and d.descuentounitario > 0]
    if not con_descuento:
        return []
    pares = variants.variantes_productos(db, sorted({d.idvar for d in con_descuento}))
    promos = catalogo.promociones(db, [producto.idpromo for _, producto in pares.values() if producto.idpromo is not None])
    agrupados = {}
    for detalle in con_descuento:
        par = pares.get(detalle.idvar)
        promo = promos.get(par[1].idpromo) if par else None
        fila = agrupados.setdefault(promo.idpromo if promo else None, dict(
            nombre=promo.nombre if promo else 'Descuento aplicado',
            tipoDescuento=promo.tipodescuento if promo else 'montoFijo',
            valorDescuento=moneda(promo.valordescuento) if promo else None,
            monto=Decimal('0.00')))
        fila['monto'] = moneda(fila['monto'] + detalle.descuentounitario * detalle.cantidad)
    return list(agrupados.values())


def reservation(db, nro, scope_id):
    """CU24: reserva que se cobra en caja (prendas, cantidades, sucursal y titular).

    El cajero no tiene permiso de CU22, así que la caja reutiliza la lectura del
    panel de sucursal (`cu22...services.reservas.cobro`) en lugar de duplicar la
    consulta o confiar en los datos que envía el navegador. Cargar la reserva es
    de sólo lectura: no descuenta stock ni cambia su estado.
    """
    return reservas.cobro(db, nro, scope_id)


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


def register_client(db, data, settings, passwords, actor, peer):
    """CU24: alta de cliente desde caja, sin cambiar la sesión del cajero.

    Reutiliza la creación interna de CU01/CU07 (procedimiento almacenado en
    PostgreSQL) pero no emite cookie: la sesión de caja permanece intacta.
    """
    email = str(data.correo)
    encoded = passwords.hash(data.contrasena.get_secret_value())
    user_id = str(uuid4())
    with transaction(db):
        role = roles_repo.public_role(db, settings.cliente_rol_id)
        if role is None:
            raise DomainError(503, 'registro_no_disponible', 'El registro no está disponible')
        if any(match.idusuario != user_id for match in users_repo.by_email(db, email)):
            raise DomainError(409, 'correo_duplicado', 'El correo ya está registrado')
        users_repo.create_client(db, user_id=user_id, data=data, password_hash=encoded,
                                 role_id=role.nro, client_code=secrets.token_hex(5))
        record(db, 'cliente_registrado', actor, peer, True)
    return dict(idUsuario=user_id, nombre=data.nombre, correo=email, ci=data.ci)


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
        client = db.get(Cliente, data.idCliente) if data.idCliente else None
        if data.idCliente and client is None:
            raise DomainError(404, 'cliente_no_encontrado', 'Selecciona un cliente registrado')
        if client is not None and client.estado == 'inactivo':
            # CU07: un cliente dado de baja no puede generar ventas nuevas.
            raise DomainError(409, 'cliente_inactivo', 'El cliente está dado de baja')
        store = db.get(Sucursal, data.nroSuc)
        if store is None or store.estado != 'activo':
            raise DomainError(404, 'sucursal_no_encontrada', 'Sucursal no disponible')
        # CU22 → CU24: la reserva es la fuente de verdad de la venta que la cobra.
        reservado: dict[str, int] = {}
        retenido: dict[str, int] = {}
        if data.nroReserva:
            if client is None:
                # CU24: una reserva pertenece a un cliente; no admite venta anónima.
                raise DomainError(422, 'cliente_requerido', 'Indica el cliente de la reserva')
            row = db.scalar(select(Reserva).where(Reserva.nroreserva == data.nroReserva).with_for_update())
            if not row or row.idusuariocl != data.idCliente or row.nrosuc != data.nroSuc:
                raise DomainError(404, 'reserva_no_encontrada', 'La reserva no corresponde a este cliente y sucursal')
            if row.estado not in reservas.ESTADOS_COBRO:
                raise DomainError(409, 'reserva_no_preparada', 'Confirma las prendas preparadas de la reserva antes de cobrarla en caja')
            pendiente, cobrada = reservas.estado_cobro(db, row.nroreserva)
            if cobrada is not None:
                # Una venta ya cobrada cierra la reserva; una venta preparada se retoma.
                raise DomainError(409, 'reserva_vendida', 'Esta reserva ya fue cobrada en una venta')
            if pendiente is not None:
                # CU24: la venta ya preparada se retoma tal cual (no se duplica) para
                # que recargar la caja no deje la reserva sin poder cobrarse.
                branch(scope_id, pendiente.nrosuc)
                if user.tipo != 'A' and pendiente.idusuarioemp != actor:
                    raise DomainError(409, 'operacion_duplicada',
                                      'La venta de esta reserva pertenece a otra sesión de caja')
                return view(db, pendiente)
            for detail in variants.detalles(db, row.nroreserva):
                reservado[detail.idvar] = reservado.get(detail.idvar, 0) + detail.cantidad
            # Con la reserva 'confirmada' la disponibilidad sigue retenida (cantDisp ya
            # descontado): esas unidades cuentan para su propio cobro, de modo que cobrar
            # la última unidad reservada no exige disponibilidad adicional.
            retenido = dict(reservado) if row.estado == 'confirmada' else {}
        quantities = {}
        for item in data.items:
            quantities[item.idVar] = quantities.get(item.idVar, 0) + item.cantidad
        faltantes = [f'{var} (reservadas {qty})' for var, qty in sorted(reservado.items())
                     if quantities.get(var, 0) < qty]
        if faltantes:
            # CU24: la venta de una reserva no puede omitir ni recortar lo reservado.
            raise DomainError(409, 'reserva_incompleta',
                              'La venta debe incluir las prendas reservadas: ' + ', '.join(faltantes))
        found = variants.variantes_productos(db, sorted(quantities))
        promos = catalogo.promociones(db, [p.idpromo for _, p in found.values()])
        gross = discount = Decimal('0')
        details = []
        for key, quantity in sorted(quantities.items()):
            pair = found.get(key)
            if not pair or any(p.estado != 'activo' for p in pair):
                raise DomainError(404, 'producto_no_encontrado', 'Una prenda ya no está disponible')
            rows = comercio.locked_inventarios(db, key, data.nroSuc)
            disponible = sum(r.cantdisp for r in rows) + retenido.get(key, 0)
            if min(disponible, sum(r.stock for r in rows)) < quantity:
                raise DomainError(409, 'disponibilidad_insuficiente', 'No hay unidades suficientes en esta sucursal')
            variant, product = pair
            base = moneda(variant.precio)
            gross += base * quantity
            discount += descuento_unitario(base, promos.get(product.idpromo)) * quantity
            details.append((key, quantity, base, descuento_unitario(base, promos.get(product.idpromo))))
        row = Venta(idusuariocl=client.idusuario if client else None, idusuarioemp=actor if user.tipo == 'E' else None, nrosuc=data.nroSuc, nit=data.nit, nroreserva=data.nroReserva, claveoperacion=str(data.claveOperacion), estado='registrada', total=moneda(gross-discount), desc_aplicado=moneda(discount))
        db.add(row); db.flush()
        # fechahora y nroventa los genera la base de datos; se refrescan antes de
        # construir la vista para no serializar un nroVenta/fecha nulos.
        db.refresh(row, attribute_names=['nroventa', 'fechahora'])
        for i, (key, qty, base, disc) in enumerate(details, 1):
            db.add(DetalleVenta(nroventa=row.nroventa, iddetalleventa=i, idvar=key, cantidad=qty, preciounitario=base, descuentounitario=disc))
        db.flush(); record(db, 'venta_caja_preparada', actor, peer, True)
        return view(db, row, actor)


def cash(db, nro, received, actor, scope_id, peer):
    with transaction(db):
        row = get(db, nro, scope_id, True)
        paid = list(db.scalars(select(Pago).where(Pago.nroventa == nro)))
        approved = next((p for p in paid if p.estado == 'aprobado'), None)
        if approved:
            return view(db, row, actor)
        if row.estado != 'registrada' or any(p.estado == 'pendiente' for p in paid):
            raise DomainError(409, 'pago_en_curso', 'La venta fue cancelada o tiene un pago en curso')
        if received < row.total:
            raise DomainError(422, 'efectivo_insuficiente', 'El efectivo recibido no cubre el total')
        payments._descontar(db, row, row.idusuariocl, peer)
        db.add(Pago(nroventa=nro, metodo='efectivo', monto=row.total, estado='aprobado', referencia=f'efectivo:recibido={received}'))
        db.flush(); record(db, 'venta_caja_pagada', actor, peer, True)
        value = view(db, row, actor); value['cambio'] = moneda(received-row.total)
        return value


def electronic(db, nro, metodo, actor, scope_id, peer, app):
    row = get(db, nro, scope_id)
    owner = row.idusuariocl
    db.commit()  # No remote request inside a transaction.
    data = PagoCrear(nroVenta=nro, metodo=metodo)
    try:
        if app.state.settings.payments_provider == 'stripe':
            if app.state.stripe is None:
                # CU24: sin pasarela real no se inventa una URL de cobro.
                raise DomainError(503, 'pasarela_no_disponible',
                                  'La pasarela Stripe no está configurada en el servidor')
            result, _ = checkout_stripe.start(db, data, owner, peer, app.state.stripe)
        else:
            if app.state.settings.environment == 'production':
                raise DomainError(503, 'pasarela_no_configurada', 'Configura una pasarela real antes de cobrar')
            result, _ = payments.iniciar(db, data, owner, peer, app.state.pasarela, app.state.settings.environment)
    except RuntimeError:
        # Fallo de comunicación con la pasarela: el pago queda pendiente y se
        # reconcilia con 'consultar-pago'; el error real no se oculta.
        raise DomainError(503, 'pasarela_no_disponible',
                          'No se pudo comunicar con la pasarela; consulta el estado del pago') from None
    with transaction(db):
        record(db, 'venta_caja_pago_electronico', actor, peer, True)
    return result
