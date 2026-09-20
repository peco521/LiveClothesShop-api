"""CU25 · Dashboard gerencial y reporte de ventas (PDF y Excel).

El dashboard, el PDF y el Excel consumen la **misma** estructura de datos que
arma ``report_model``: los indicadores, el reporte matricial y las exportaciones
nunca muestran cifras distintas para los mismos filtros.

Reglas de negocio de este módulo:

- La tabla ``categoria`` representa el **modelo/estilo de polera** (Deportiva,
  Semi-Formal, ...). El parámetro ``idCat`` es el filtro "Modelo de polera".
- La temporada no vive en ``producto``: se relaciona con su colección mediante
  ``tempcoleccion``.
- "Ventas pagadas" cuenta ventas ``registrada`` (las anuladas quedan fuera) con
  al menos un pago ``aprobado``; los pagos pendientes o rechazados no cuentan.
- "Poleras vendidas" e "Importe total" salen de las líneas de ``detalleventa``
  que cumplen los filtros, de modo que una venta con poleras de varios modelos
  aporta a cada modelo solo su parte:
  (``precioUnitario - descuentoUnitario``) * ``cantidad``.
- Las ventas nunca se duplican por los joins con ``detalleventa``: se cuentan en
  una consulta a nivel de ``venta`` y las cantidades/importes en otra por línea.
"""
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import String, cast, func, select
from sqlalchemy.orm import aliased

from app.core.errors import DomainError
from app.modules.cliente_experiencia_compra.shared.models.catalogo import (
    Categoria, Inventario, Producto, TempColeccion, Temporada, VarianteProd,
)
from app.modules.cliente_experiencia_compra.shared.models.comercio import (
    Carrito, DetalleReserva, DetalleVenta, Pago, Reserva, Venta,
)
from app.modules.inventario_productos.cu23_gestionar_devoluciones.models.devoluciones import (
    Devolucion, DetalleDev, Reembolso,
)
from app.modules.seguridad_accesos.models import Usuario
from app.modules.seguridad_accesos.shared.models import Sucursal

CENTAVO = Decimal('0.01')
CERO = Decimal('0.00')
MESES = ('Ene', 'Feb', 'Mar', 'Abr', 'May', 'Jun', 'Jul', 'Ago', 'Sep', 'Oct', 'Nov', 'Dic')
DIAS_COLUMNA_DIARIA = 31          # hasta 31 días el reporte usa una columna por día
FILAS_DETALLE_MAX = 5000          # tope de filas de la hoja "DETALLE DE VENTAS"
ZONA_POR_DEFECTO = 'America/La_Paz'   # los reportes agrupan por el día/mes local del negocio
COLUMNAS_DEVOLUCIONES = (('Nro. Devolución', 'entero', 13), ('Venta', 'entero', 9), ('Fecha', 'texto', 12),
                         ('Sucursal', 'texto', 22), ('Cliente', 'texto', 24), ('Polera', 'texto', 30),
                         ('Modelo de polera', 'texto', 20), ('Temporada', 'texto', 18), ('Cantidad', 'entero', 10),
                         ('Motivo', 'texto', 28), ('Estado', 'texto', 12), ('Reembolso', 'texto', 16),
                         ('Importe devuelto', 'moneda', 15), ('Monto de la devolución', 'moneda', 16))
NOTA_DEVOLUCIONES = ('Devoluciones del período con la polera devuelta. "Importe devuelto" es el importe de la linea '
                     'vendida (precio - descuento) x cantidad; el monto reembolsado se muestra una sola vez por '
                     'devolucion para no duplicar la suma.')

TITULO_VENTAS = 'REPORTE DE VENTAS'
SIN_SUCURSAL = 'Todas las sucursales'
SIN_MODELO = 'Todos los modelos'
SIN_TEMPORADA = 'Todas las temporadas'
RESUMEN_VENTAS = (('Ventas pagadas', 'entero', 15), ('Poleras vendidas', 'entero', 17),
                  ('Importe total', 'moneda', 15), ('Ticket promedio', 'moneda', 16))
NOTA_VENTAS = ('Ventas pagadas: ventas registradas con pago aprobado. Las columnas de período muestran el '
               'importe de las poleras que cumplen los filtros; poleras vendidas = suma de cantidades.')
COLUMNAS_DETALLE = (('Nro. Venta', 'entero', 10), ('Fecha', 'texto', 12), ('Hora', 'texto', 9),
                    ('Sucursal', 'texto', 24), ('Cliente', 'texto', 26), ('Empleado', 'texto', 22),
                    ('Polera', 'texto', 30), ('Modelo de polera', 'texto', 20), ('Temporada', 'texto', 18),
                    ('Cantidad', 'entero', 10), ('Precio unitario', 'moneda', 15),
                    ('Descuento', 'moneda', 13), ('Importe', 'moneda', 15))


def _zona(nombre=None):
    """Zona horaria del negocio con respaldo en UTC si el nombre configurado no es valido."""
    try:
        return ZoneInfo(nombre or ZONA_POR_DEFECTO)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return timezone.utc


def _desfase_horas(zona, referencia):
    """Horas que hay que sumar a la hora local para obtener UTC (Bolivia: 4)."""
    return int(-datetime.combine(referencia, time(12, 0), tzinfo=zona).utcoffset().total_seconds() // 3600)


def _local(valor, zona):
    """``fechahora`` se guarda en UTC (``func.now()``); la muestra en la hora del negocio."""
    return valor.replace(tzinfo=timezone.utc).astimezone(zona)


def _rango(ini, fin, desfase=0):
    """Valida el rango y devuelve el intervalo UTC del dia local [ini 00:00, fin+1 00:00).

    ``fechahora`` se guarda en UTC, asi que un dia local (Bolivia, UTC-4) abarca desde
    las 04:00 UTC de ese dia hasta las 04:00 UTC del siguiente.
    """
    if fin < ini:
        raise DomainError(422, 'rango_invalido', 'La fecha final no puede ser anterior a la inicial')
    origen = timedelta(hours=desfase)
    return (datetime.combine(ini, time.min) + origen,
            datetime.combine(fin + timedelta(days=1), time.min) + origen)


def _variantes(db, cat=None, temp=None):
    """Ids de variante de las poleras que cumplen el modelo y la temporada."""
    products = select(Producto.idprod)
    if cat is not None:
        products = products.where(Producto.idcat == cat)
    if temp is not None:
        products = products.where(select(TempColeccion.idcol).where(
            TempColeccion.idcol == Producto.idcol, TempColeccion.idtemp == temp).exists())
    # Los ids de variante se materializan: las consultas externas ya incluyen
    # producto/varianteprod y la autocorrelación dejaría las subconsultas sin FROM.
    return list(db.scalars(select(VarianteProd.idvariante).where(VarianteProd.idprod.in_(products))))


def _alcance(ini, fin, suc=None, variantes=(), filtrado=False, desfase=0):
    """Condiciones sobre ``venta``: rango, sucursal y venta pagada (+ modelo/temporada)."""
    start, end = _rango(ini, fin, desfase)
    conditions = [Venta.fechahora >= start, Venta.fechahora < end, Venta.estado == 'registrada',
                  select(Pago.idpago).where(Pago.nroventa == Venta.nroventa, Pago.estado == 'aprobado').exists()]
    if suc is not None:
        conditions.append(Venta.nrosuc == suc)
    if filtrado:
        # Solo Venta se correlaciona: DetalleVenta debe permanecer en el FROM
        # aunque la consulta externa (p. ej. el ranking de más vendidos) ya la incluya.
        conditions.append(select(DetalleVenta.nroventa).where(
            DetalleVenta.nroventa == Venta.nroventa, DetalleVenta.idvar.in_(variantes)).correlate(Venta).exists())
    return conditions


def _alcance_lineas(ini, fin, suc=None, variantes=(), desfase=0):
    """Condiciones de las líneas (``detalleventa``) de ventas pagadas y filtradas."""
    start, end = _rango(ini, fin, desfase)
    conditions = [Venta.fechahora >= start, Venta.fechahora < end, Venta.estado == 'registrada',
                  select(Pago.idpago).where(Pago.nroventa == Venta.nroventa, Pago.estado == 'aprobado').exists(),
                  DetalleVenta.idvar.in_(variantes)]
    if suc is not None:
        conditions.append(Venta.nrosuc == suc)
    return conditions


def _importe_linea():
    """Importe de una línea: (precioUnitario - descuentoUnitario) * cantidad."""
    return (DetalleVenta.preciounitario - func.coalesce(DetalleVenta.descuentounitario, 0)) * DetalleVenta.cantidad


def _clave_periodo(db, columna, granularidad, zona=None, desfase=0):
    """Clave 'YYYY-MM-DD' (dia) o 'YYYY-MM' (mes) de la fecha local del negocio.

    ``fechahora`` se guarda en UTC: en SQLite (pruebas) se desplaza con
    ``datetime(x, '-4 hours')`` y en PostgreSQL se interpreta como UTC y se convierte
    a la zona configurada, para que una venta de las 20:00 locales caiga en su dia.
    """
    if not desfase:
        desplazada = columna
    elif db.get_bind().dialect.name == 'sqlite':
        desplazada = func.datetime(columna, f'{int(-desfase)} hours')
    else:
        desplazada = func.timezone(str(zona), func.timezone('UTC', columna))
    return func.substr(cast(desplazada, String), 1, 10 if granularidad == 'dia' else 7)


def _decimal(value, default=CERO):
    return default if value is None else Decimal(str(value))


def _redondear(value):
    return _decimal(value).quantize(CENTAVO, rounding=ROUND_HALF_UP)


def _ticket(importe, ventas):
    """Ticket promedio = importe total / ventas pagadas (0.00 si no hubo ventas)."""
    return _redondear(_decimal(importe) / ventas) if ventas else CERO


def _fecha_texto(value):
    return value.strftime('%d/%m/%Y')


def _periodos(ini, fin):
    """Columnas dinámicas: un día por columna hasta 31 días; por mes si el rango es mayor."""
    if (fin - ini).days + 1 <= DIAS_COLUMNA_DIARIA:
        dias = [ini + timedelta(days=offset) for offset in range((fin - ini).days + 1)]
        return 'dia', [(dia.isoformat(), f'{dia.day:02d} {MESES[dia.month - 1]}') for dia in dias]
    anios, periodos, cursor = {ini.year, fin.year}, [], date(ini.year, ini.month, 1)
    while cursor <= fin:
        etiqueta = MESES[cursor.month - 1] if len(anios) == 1 else f'{MESES[cursor.month - 1]} {cursor.year}'
        periodos.append((f'{cursor.year:04d}-{cursor.month:02d}', etiqueta))
        cursor = date(cursor.year + 1, 1, 1) if cursor.month == 12 else date(cursor.year, cursor.month + 1, 1)
    return 'mes', periodos


def _nombres_filtros(db, suc=None, cat=None, temp=None):
    """Etiqueta visible de cada filtro (nombre real; "todos" cuando no hay filtro)."""
    sucursal = db.scalar(select(Sucursal.nombre).where(Sucursal.nro == suc)) if suc is not None else None
    categoria = db.scalar(select(Categoria.descripcion).where(Categoria.idcat == cat)) if cat is not None else None
    temporada = db.scalar(select(Temporada.nombre).where(Temporada.idtemp == temp)) if temp is not None else None
    return dict(sucursal=sucursal or SIN_SUCURSAL, modelo=categoria or SIN_MODELO, temporada=temporada or SIN_TEMPORADA)


def _agregado_sucursales(db, ini, fin, suc, variantes, filtrado, desfase=0):
    """Ventas pagadas por sucursal (a nivel de ``venta``: el detalle nunca la duplica)."""
    rows = db.execute(select(Sucursal.nro, Sucursal.nombre, func.count(Venta.nroventa))
                      .join(Venta, Venta.nrosuc == Sucursal.nro)
                      .where(*_alcance(ini, fin, suc, variantes, filtrado, desfase))
                      .group_by(Sucursal.nro, Sucursal.nombre)).all()
    return {row[0]: dict(nombre=row[1], ventas=row[2]) for row in rows}


def _agregado_lineas(db, ini, fin, suc, variantes, granularidad, zona=None, desfase=0):
    """Poleras vendidas e importe por sucursal y período (a nivel de ``detalleventa``)."""
    clave = _clave_periodo(db, Venta.fechahora, granularidad, zona, desfase)
    rows = db.execute(select(Sucursal.nro, clave, func.sum(DetalleVenta.cantidad), func.sum(_importe_linea()))
                      .select_from(DetalleVenta)
                      .join(Venta, Venta.nroventa == DetalleVenta.nroventa)
                      .join(Sucursal, Sucursal.nro == Venta.nrosuc)
                      .where(*_alcance_lineas(ini, fin, suc, variantes, desfase))
                      .group_by(Sucursal.nro, clave)).all()
    return {(row[0], row[1]): (int(row[2] or 0), _decimal(row[3])) for row in rows}


def _matriz(db, ini, fin, suc=None, cat=None, temp=None, variantes=None, zona=None, desfase=0):
    """Matriz del reporte: una fila por sucursal y una columna por día o mes local."""
    granularidad, periodos = _periodos(ini, fin)
    filtrado = cat is not None or temp is not None
    variantes = _variantes(db, cat, temp) if variantes is None else variantes
    sucursales = _agregado_sucursales(db, ini, fin, suc, variantes, filtrado, desfase)
    if suc is not None and suc not in sucursales:
        # La sucursal pedida se muestra aunque no tenga ventas (sus cifras quedan en cero).
        nombre = db.scalar(select(Sucursal.nombre).where(Sucursal.nro == suc))
        if nombre is not None:
            sucursales[suc] = dict(nombre=nombre, ventas=0)
    lineas = _agregado_lineas(db, ini, fin, suc, variantes, granularidad, zona, desfase)
    filas = []
    for nro in sorted(sucursales, key=lambda item: sucursales[item]['nombre']):
        celdas = [lineas.get((nro, clave), (0, CERO)) for clave, _ in periodos]
        importe = sum((celda[1] for celda in celdas), CERO)
        ventas = sucursales[nro]['ventas']
        filas.append(dict(sucursal=sucursales[nro]['nombre'], valores=[_redondear(celda[1]) for celda in celdas],
                          ventas=ventas, poleras=sum(celda[0] for celda in celdas),
                          importe=_redondear(importe), ticket=_ticket(importe, ventas)))
    total = dict(sucursal='TOTAL GENERAL',
                 valores=[_redondear(sum((fila['valores'][posicion] for fila in filas), CERO))
                          for posicion in range(len(periodos))],
                 ventas=sum(fila['ventas'] for fila in filas), poleras=sum(fila['poleras'] for fila in filas),
                 importe=_redondear(sum((fila['importe'] for fila in filas), CERO)))
    total['ticket'] = _ticket(total['importe'], total['ventas'])
    return dict(granularidad=granularidad,
                periodos=[dict(clave=clave, etiqueta=etiqueta) for clave, etiqueta in periodos],
                filas=filas, total=total)


def _detalle_devoluciones(db, ini, fin, suc=None, cat=None, temp=None, zona=None, desfase=0):
    """Poleras devueltas que cumplen los filtros, con el motivo y el monto de la devolucion.

    Alimenta el indicador "Devoluciones", la tabla del panel y las exportaciones: una
    fila por polera devuelta, unida a su linea de venta (precio y descuento reales).
    """
    franja = zona or _zona()
    start, end = _rango(ini, fin, desfase)
    variantes = _variantes(db, cat, temp)
    cliente, devolucion_reembolso = aliased(Usuario), aliased(Reembolso)
    consulta = (select(Devolucion.nrodev, Devolucion.nroventa, Devolucion.fechahora, Devolucion.estado,
                       Devolucion.monto, Devolucion.motivo, Sucursal.nombre,
                       cliente.nombre, cliente.apellidopat, cliente.apellidomat,
                       Producto.descripcion, Categoria.descripcion, Producto.idcol, DetalleDev.iddetalledev,
                       DetalleDev.cantidad, DetalleVenta.preciounitario,
                       func.coalesce(DetalleVenta.descuentounitario, 0),
                       devolucion_reembolso.estado, devolucion_reembolso.monto)
                .select_from(Devolucion)
                .join(Venta, Venta.nroventa == Devolucion.nroventa)
                .join(Sucursal, Sucursal.nro == Venta.nrosuc)
                .join(DetalleDev, DetalleDev.nrodev == Devolucion.nrodev)
                .join(DetalleVenta, (DetalleVenta.nroventa == DetalleDev.nroventa)
                      & (DetalleVenta.iddetalleventa == DetalleDev.iddetalleventa))
                .join(VarianteProd, VarianteProd.idvariante == DetalleVenta.idvar)
                .join(Producto, Producto.idprod == VarianteProd.idprod)
                .join(Categoria, Categoria.idcat == Producto.idcat)
                .outerjoin(cliente, cliente.idusuario == Venta.idusuariocl)
                .outerjoin(devolucion_reembolso, devolucion_reembolso.nrodev == Devolucion.nrodev)
                .where(Devolucion.fechahora >= start, Devolucion.fechahora < end,
                       DetalleVenta.idvar.in_(variantes))
                .order_by(Devolucion.nrodev, DetalleDev.iddetalledev))
    if suc is not None:
        consulta = consulta.where(Venta.nrosuc == suc)
    temporadas = _temporadas_por_coleccion(db)
    lineas = []
    for row in db.execute(consulta).all():
        fecha = _local(row[2], franja)
        lineas.append(dict(nroDev=row[0], nroVenta=row[1], fecha=fecha.strftime('%d/%m/%Y'),
                           hora=fecha.strftime('%H:%M'), estadoDev=row[3], monto=_redondear(row[4]),
                           motivo=row[5] or '', sucursal=row[6],
                           cliente=_nombre_completo(row[7], row[8], row[9]) or 'Consumidor final',
                           producto=row[10], modelo=row[11], temporada=temporadas.get(row[12], ''),
                           cantidad=row[14], precioUnitario=_redondear(row[15]), descuento=_redondear(row[16]),
                           importeLinea=_redondear((_decimal(row[15]) - _decimal(row[16])) * row[14]),
                           estadoReembolso=row[17] or 'sin reembolso',
                           montoReembolso=_redondear(row[18]) if row[18] is not None else CERO))
    return lineas


def _devoluciones_agrupadas(lineas):
    """Agrupa las poleras devueltas: una fila por devolucion con las poleras concatenadas."""
    agrupadas = {}
    for linea in lineas:
        fila = agrupadas.get(linea['nroDev'])
        if fila is None:
            fila = dict(nroDev=linea['nroDev'], nroVenta=linea['nroVenta'], fecha=linea['fecha'],
                        hora=linea['hora'], sucursal=linea['sucursal'], cliente=linea['cliente'],
                        motivo=linea['motivo'], estado=linea['estadoDev'], monto=linea['monto'],
                        reembolso=linea['estadoReembolso'], poleras=[], cantidad=0, importeLinea=CERO)
            agrupadas[linea['nroDev']] = fila
        fila['poleras'].append(f"{linea['producto']} x{linea['cantidad']}")
        fila['cantidad'] += linea['cantidad']
        fila['importeLinea'] = _redondear(fila['importeLinea'] + linea['importeLinea'])
    for fila in agrupadas.values():
        fila['poleras'] = ', '.join(fila['poleras'])
    return sorted(agrupadas.values(), key=lambda fila: fila['nroDev'])


def dashboard(db, ini, fin, suc=None, cat=None, temp=None, zona=None):
    franja = _zona(zona)
    desfase = _desfase_horas(franja, ini)
    start, end = _rango(ini, fin, desfase)
    variant_ids = _variantes(db, cat, temp)
    filtrado = cat is not None or temp is not None
    scope = _alcance(ini, fin, suc, variant_ids, filtrado, desfase)
    # El reporte matricial y los indicadores comparten esta misma base de cálculo.
    matriz = _matriz(db, ini, fin, suc, cat, temp, variant_ids, franja, desfase)
    by_branch = [dict(nombre=fila['sucursal'], ventas=fila['ventas'], total=fila['importe'],
                      poleras=fila['poleras'], ticket=fila['ticket']) for fila in matriz['filas']]
    sold = db.execute(select(Producto.descripcion, func.sum(DetalleVenta.cantidad)).join(VarianteProd, VarianteProd.idvariante == DetalleVenta.idvar).join(Producto, Producto.idprod == VarianteProd.idprod).join(Venta, Venta.nroventa == DetalleVenta.nroventa).where(*scope, DetalleVenta.idvar.in_(variant_ids)).group_by(Producto.idprod, Producto.descripcion).order_by(func.sum(DetalleVenta.cantidad).desc()).limit(10)).all()
    inv_conditions = [Inventario.idvar.in_(variant_ids)]
    if suc is not None:
        inv_conditions.append(Inventario.nrosuc == suc)
    inventory = db.execute(select(func.coalesce(func.sum(Inventario.stock), 0), func.coalesce(func.sum(Inventario.cantdisp), 0), func.count().filter(Inventario.cantdisp == 0)).where(*inv_conditions)).one()
    res_conditions = [Reserva.fechareserva >= ini, Reserva.fechareserva <= fin]
    if suc is not None:
        res_conditions.append(Reserva.nrosuc == suc)
    from app.modules.cliente_experiencia_compra.shared.models.comercio import DetalleReserva
    if cat is not None or temp is not None:
        res_conditions.append(select(DetalleReserva.nroreserva).where(DetalleReserva.nroreserva == Reserva.nroreserva, DetalleReserva.idvar.in_(variant_ids)).exists())
    reservations = db.scalar(select(func.count()).select_from(Reserva).where(*res_conditions))
    converted = db.scalar(select(func.count()).select_from(Reserva).where(*res_conditions, select(Venta.nroventa).where(Venta.nroreserva == Reserva.nroreserva, select(Pago.idpago).where(Pago.nroventa == Venta.nroventa, Pago.estado == 'aprobado').exists()).exists()))
    # Cart has no branch: denominator is clients' carts tied to a sale in branch when filtering.
    cart_conditions = [Carrito.fechahora >= start, Carrito.fechahora < end]
    if suc is not None or cat is not None or temp is not None:
        cart_sale = select(Venta.nroventa).where(Venta.idcarrito == Carrito.idcarrito)
        if suc is not None:
            cart_sale = cart_sale.where(Venta.nrosuc == suc)
        if cat is not None or temp is not None:
            cart_sale = cart_sale.where(select(DetalleVenta.nroventa).where(DetalleVenta.nroventa == Venta.nroventa, DetalleVenta.idvar.in_(variant_ids)).exists())
        cart_conditions.append(cart_sale.exists())
    carts = db.scalar(select(func.count()).select_from(Carrito).where(*cart_conditions))
    closed = db.scalar(select(func.count()).select_from(Carrito).where(*cart_conditions, Carrito.estado == 'convertido'))
    # El indicador y la tabla de devoluciones salen de la misma consulta: nunca se
    # contradicen y respetan rango, sucursal, modelo y temporada (incluida la polera
    # devuelta), con las horas guardadas en UTC convertidas a la hora del negocio.
    devoluciones_resumen = _devoluciones_agrupadas(_detalle_devoluciones(db, ini, fin, suc, cat, temp, franja, desfase))
    return dict(ventas=matriz['total']['ventas'], ingresos=matriz['total']['importe'],
                poleras=matriz['total']['poleras'], ticketPromedio=matriz['total']['ticket'],
                sucursales=by_branch, productos=[dict(nombre=r[0], unidades=r[1]) for r in sold],
                inventario=dict(stock=inventory[0], disponible=inventory[1], reservado=inventory[0]-inventory[1], agotados=inventory[2]),
                reservas=reservations, conversionReservas=round(100*converted/reservations, 2) if reservations else 0,
                carritos=carts, conversionCarritos=round(100*closed/carts, 2) if carts else 0,
                devoluciones=len(devoluciones_resumen), devolucionesDetalle=devoluciones_resumen,
                reporte=matriz, filtros=_nombres_filtros(db, suc, cat, temp),
                nota='Ventas pagadas: ventas registradas con pago aprobado. El importe y las poleras vendidas consideran solo las líneas que cumplen el modelo y la temporada; las devoluciones incluyen la polera devuelta; los días y meses se calculan en la hora local del negocio; inventario al momento de consultar. Carritos por sucursal: solo carritos vinculados a ventas de esa sucursal.')


def _nombre_completo(nombre, apellidopat, apellidomat):
    return ' '.join(parte for parte in (nombre, apellidopat, apellidomat) if parte)


def _nombre_corto(nombre, apellidopat):
    return ' '.join(parte for parte in (nombre, apellidopat) if parte)


def _temporadas_por_coleccion(db):
    """Nombres de temporada por colección (la temporada vive en ``tempcoleccion``)."""
    nombres = {}
    for idcol, nombre in db.execute(select(TempColeccion.idcol, Temporada.nombre)
                                    .join(Temporada, Temporada.idtemp == TempColeccion.idtemp)
                                    .order_by(Temporada.nombre)).all():
        nombres.setdefault(idcol, []).append(nombre)
    return {idcol: ', '.join(valores) for idcol, valores in nombres.items()}


def _detalle_ventas(db, ini, fin, suc=None, cat=None, temp=None, zona=None, desfase=0):
    """Líneas de venta pagada que cumplen los filtros (hoja "DETALLE DE VENTAS")."""
    franja = zona or _zona()
    variantes = _variantes(db, cat, temp)
    cliente, empleado = aliased(Usuario), aliased(Usuario)
    rows = db.execute(select(Venta.nroventa, Venta.fechahora, Sucursal.nombre, cliente.nombre,
                             cliente.apellidopat, cliente.apellidomat, empleado.nombre, empleado.apellidopat,
                             Producto.descripcion, Categoria.descripcion, Producto.idcol, DetalleVenta.cantidad,
                             DetalleVenta.preciounitario, func.coalesce(DetalleVenta.descuentounitario, 0),
                             _importe_linea())
                      .select_from(DetalleVenta)
                      .join(Venta, Venta.nroventa == DetalleVenta.nroventa)
                      .join(Sucursal, Sucursal.nro == Venta.nrosuc)
                      .join(VarianteProd, VarianteProd.idvariante == DetalleVenta.idvar)
                      .join(Producto, Producto.idprod == VarianteProd.idprod)
                      .join(Categoria, Categoria.idcat == Producto.idcat)
                      .outerjoin(cliente, cliente.idusuario == Venta.idusuariocl)
                      .outerjoin(empleado, empleado.idusuario == Venta.idusuarioemp)
                      .where(*_alcance_lineas(ini, fin, suc, variantes, desfase))
                      .order_by(Venta.fechahora, Venta.nroventa, DetalleVenta.iddetalleventa)
                      .limit(FILAS_DETALLE_MAX)).all()
    temporadas = _temporadas_por_coleccion(db)
    filas = []
    for row in rows:
        fecha = _local(row[1], franja)
        filas.append([row[0], fecha.strftime('%d/%m/%Y'), fecha.strftime('%H:%M'), row[2],
                      _nombre_completo(row[3], row[4], row[5]), _nombre_corto(row[6], row[7]),
                      row[8], row[9], temporadas.get(row[10], ''), row[11], _redondear(row[12]),
                      _redondear(row[13]), _redondear(row[14])])
    nota = f'Detalle de {len(filas)} línea(s) de venta pagada con los filtros del reporte.'
    if len(filas) >= FILAS_DETALLE_MAX:
        nota = f'Detalle limitado a las primeras {FILAS_DETALLE_MAX} líneas de venta pagada.'
    return dict(filas=filas, nota=nota)


def _columnas_periodo(matriz):
    """Columnas del reporte: sucursal, un día/mes por columna y los indicadores finales."""
    columnas = [('Sucursal', 'texto', 30)]
    columnas += [(periodo['etiqueta'], 'moneda', 12) for periodo in matriz['periodos']]
    return columnas + list(RESUMEN_VENTAS)


def _modelo_ventas(db, ini, fin, suc, cat, temp, filtros, zona=None, desfase=0):
    """Matriz del reporte de ventas con la hoja de detalle adjunta."""
    matriz = _matriz(db, ini, fin, suc, cat, temp, None, zona, desfase)
    filas = [[fila['sucursal']] + fila['valores'] + [fila['ventas'], fila['poleras'], fila['importe'], fila['ticket']]
             for fila in matriz['filas']]
    total = ['TOTAL GENERAL'] + matriz['total']['valores'] + [matriz['total']['ventas'], matriz['total']['poleras'],
                                                              matriz['total']['importe'], matriz['total']['ticket']]
    detalle = _detalle_ventas(db, ini, fin, suc, cat, temp, zona, desfase)
    hojas = [dict(nombre='DETALLE DE VENTAS', titulo='DETALLE DE VENTAS', filtros=filtros,
                  columnas=list(COLUMNAS_DETALLE), filas=detalle['filas'], total=None, nota=detalle['nota'])]
    return dict(titulo=TITULO_VENTAS, filtros=filtros, columnas=_columnas_periodo(matriz), filas=filas,
                total=total, hojas=hojas, granularidad=matriz['granularidad'], periodos=matriz['periodos'],
                nota=NOTA_VENTAS)


def _modelo_devoluciones(db, ini, fin, suc, cat, temp, filtros, zona=None, desfase=0):
    """Reporte de devoluciones: una fila por polera devuelta, con su motivo y su monto.

    El monto de la devolución se escribe solo en la primera línea de cada devolución
    para que la suma de la columna sea el total reembolsado real.
    """
    franja = zona or _zona()
    lineas = _detalle_devoluciones(db, ini, fin, suc, cat, temp, franja, desfase)
    filas, devoluciones, poleras, importe, monto = [], set(), 0, CERO, CERO
    for linea in lineas:
        primera = linea['nroDev'] not in devoluciones
        devoluciones.add(linea['nroDev'])
        filas.append([linea['nroDev'], linea['nroVenta'], linea['fecha'], linea['sucursal'], linea['cliente'],
                      linea['producto'], linea['modelo'], linea['temporada'], linea['cantidad'], linea['motivo'],
                      linea['estadoDev'], linea['estadoReembolso'], linea['importeLinea'],
                      linea['monto'] if primera else ''])
        poleras += linea['cantidad']
        importe = _redondear(importe + linea['importeLinea'])
        if primera:
            monto = _redondear(monto + linea['monto'])
    total = ['TOTAL GENERAL', '', '', '', '', f'{len(devoluciones)} devolución(es)', '', '', poleras, '', '', '',
             importe, monto]
    return dict(titulo='REPORTE DE DEVOLUCIONES', filtros=filtros, columnas=list(COLUMNAS_DEVOLUCIONES),
                filas=filas, total=total, hojas=[], nota=NOTA_DEVOLUCIONES)


def report_model(db, kind, ini, fin, suc=None, cat=None, temp=None, zona=None):
    """Estructura común del reporte: el PDF y el Excel la consumen sin recalcular nada.

    El tipo ``ventas`` devuelve la matriz (sucursales x días/meses + indicadores) y
    ``devoluciones`` el detalle de las poleras devueltas; el resto conserva sus
    indicadores en una tabla simple.
    """
    franja = _zona(zona)
    desfase = _desfase_horas(franja, ini)
    _rango(ini, fin, desfase)
    nombres = _nombres_filtros(db, suc, cat, temp)
    filtros = [('Período:', f'{_fecha_texto(ini)} - {_fecha_texto(fin)}'),
               ('Sucursal:', nombres['sucursal']),
               ('Modelo de polera:', nombres['modelo']),
               ('Temporada:', nombres['temporada']),
               ('Generado:', datetime.now().strftime('%d/%m/%Y %H:%M'))]
    if kind == 'ventas':
        return _modelo_ventas(db, ini, fin, suc, cat, temp, filtros, franja, desfase)
    if kind == 'devoluciones':
        return _modelo_devoluciones(db, ini, fin, suc, cat, temp, filtros, franja, desfase)
    data = dashboard(db, ini, fin, suc, cat, temp, franja)
    simple = [('Indicador', 'texto', 30), ('Valor', 'entero', 14)]
    if kind == 'inventario':
        return dict(titulo='REPORTE DE INVENTARIO', filtros=filtros, columnas=simple,
                    filas=[[clave, valor] for clave, valor in data['inventario'].items()], total=None, hojas=[],
                    nota='Inventario de las poleras que cumplen los filtros, al momento de consultar.')
    if kind == 'reservas':
        return dict(titulo='REPORTE DE RESERVAS', filtros=filtros, columnas=simple,
                    filas=[['Reservas del período', data['reservas']],
                           ['Conversión a venta (%)', data['conversionReservas']]], total=None, hojas=[],
                    nota='Reservas del período para la sucursal y los filtros seleccionados.')
    # El router solo admite 'ventas', 'inventario', 'reservas' y 'devoluciones'.
    raise DomainError(400, 'reporte_desconocido', f'Tipo de reporte no soportado: {kind}')


_XML = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
_NS_MAIN = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
_NS_REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
_NS_PKG = 'http://schemas.openxmlformats.org/package/2006/relationships'
_RELACIONES_RAIZ = (_XML + f'<Relationships xmlns="{_NS_PKG}">'
                    f'<Relationship Id="rId1" Type="{_NS_REL}/officeDocument" Target="xl/workbook.xml"/>'
                    '</Relationships>')
# Formatos de celda del libro: 0 base, 1 título, 2 etiqueta de filtro, 3 valor de filtro,
# 4 encabezado, 5 texto, 6 moneda (#,##0.00), 7 entero (#,##0), 8..10 totales, 11 nota.
_ESTILOS_XML = _XML + (
    f'<styleSheet xmlns="{_NS_MAIN}">'
    '<fonts count="6">'
    '<font><sz val="11"/><name val="Calibri"/></font>'
    '<font><b/><sz val="16"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font>'
    '<font><b/><sz val="11"/><name val="Calibri"/></font>'
    '<font><sz val="10"/><name val="Calibri"/></font>'
    '<font><b/><sz val="10"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font>'
    '<font><i/><sz val="9"/><color rgb="FF595959"/><name val="Calibri"/></font>'
    '</fonts>'
    '<fills count="5">'
    '<fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill>'
    '<fill><patternFill patternType="solid"><fgColor rgb="FF1F3864"/><bgColor indexed="64"/></patternFill></fill>'
    '<fill><patternFill patternType="solid"><fgColor rgb="FFDCE6F1"/><bgColor indexed="64"/></patternFill></fill>'
    '<fill><patternFill patternType="solid"><fgColor rgb="FFF2F2F2"/><bgColor indexed="64"/></patternFill></fill>'
    '</fills>'
    '<borders count="3">'
    '<border><left/><right/><top/><bottom/><diagonal/></border>'
    '<border><left style="thin"><color rgb="FFB4C6E7"/></left><right style="thin"><color rgb="FFB4C6E7"/></right>'
    '<top style="thin"><color rgb="FFB4C6E7"/></top><bottom style="thin"><color rgb="FFB4C6E7"/></bottom>'
    '<diagonal/></border>'
    '<border><left style="thin"><color rgb="FF8EA9DB"/></left><right style="thin"><color rgb="FF8EA9DB"/></right>'
    '<top style="double"><color rgb="FF1F3864"/></top><bottom style="thin"><color rgb="FF8EA9DB"/></bottom>'
    '<diagonal/></border>'
    '</borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="12">'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1" '
    'applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>'
    '<xf numFmtId="0" fontId="2" fillId="4" borderId="0" xfId="0" applyFont="1" applyFill="1"/>'
    '<xf numFmtId="0" fontId="3" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
    '<xf numFmtId="0" fontId="4" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" '
    'applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>'
    '<xf numFmtId="0" fontId="3" fillId="0" borderId="1" xfId="0" applyFont="1" applyBorder="1" '
    'applyAlignment="1"><alignment horizontal="left" vertical="center"/></xf>'
    '<xf numFmtId="4" fontId="3" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyFont="1" '
    'applyBorder="1" applyAlignment="1"><alignment horizontal="right" vertical="center"/></xf>'
    '<xf numFmtId="3" fontId="3" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyFont="1" '
    'applyBorder="1" applyAlignment="1"><alignment horizontal="right" vertical="center"/></xf>'
    '<xf numFmtId="0" fontId="2" fillId="3" borderId="2" xfId="0" applyFont="1" applyFill="1" '
    'applyBorder="1" applyAlignment="1"><alignment horizontal="left" vertical="center"/></xf>'
    '<xf numFmtId="4" fontId="2" fillId="3" borderId="2" xfId="0" applyNumberFormat="1" applyFont="1" '
    'applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="right" vertical="center"/></xf>'
    '<xf numFmtId="3" fontId="2" fillId="3" borderId="2" xfId="0" applyNumberFormat="1" applyFont="1" '
    'applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="right" vertical="center"/></xf>'
    '<xf numFmtId="0" fontId="5" fillId="0" borderId="0" xfId="0" applyFont="1">'
    '<alignment horizontal="left" vertical="center"/></xf>'
    '</cellXfs>'
    '</styleSheet>'
)
_INDICES_ESTILO = dict(base=0, titulo=1, etiqueta=2, valor=3, encabezado=4, texto=5, moneda=6, entero=7,
                       totalTexto=8, totalMoneda=9, totalEntero=10, nota=11)


def _letra(numero):
    """Letra(s) de columna de Excel para un número de columna 1..N."""
    letras = ''
    while numero:
        numero, resto = divmod(numero - 1, 26)
        letras = chr(65 + resto) + letras
    return letras


def _numero_xml(valor):
    return f'{valor:.2f}' if isinstance(valor, Decimal) else str(valor)


def _celda_xlsx(fila, columna, valor, estilo, tipo=None):
    """Celda numérica (importes y cantidades reales) o de texto."""
    referencia = f'{_letra(columna)}{fila}'
    if tipo != 'texto' and isinstance(valor, (int, Decimal)) and not isinstance(valor, bool):
        return f'<c r="{referencia}" s="{estilo}"><v>{_numero_xml(valor)}</v></c>'
    return (f'<c r="{referencia}" s="{estilo}" t="inlineStr"><is><t xml:space="preserve">'
            f'{escape(str(valor if valor is not None else ""))}</t></is></c>')


def _fila_xlsx(numero, celdas, alto=None):
    atributo = f' ht="{alto}" customHeight="1"' if alto else ''
    contenido = ''.join(_celda_xlsx(numero, posicion, *celda) for posicion, celda in enumerate(celdas, 1))
    return f'<row r="{numero}"{atributo}>{contenido}</row>'


def _estilo_dato(tipo):
    return tipo if tipo in ('moneda', 'entero') else 'texto'


def _estilo_total(tipo):
    return dict(moneda='totalMoneda', entero='totalEntero').get(tipo, 'totalTexto')


def _hoja_xlsx(hoja, estilo):
    """Hoja de cálculo: título, filtros, encabezado congelado, datos, total y nota."""
    columnas, filas, total = hoja['columnas'], hoja['filas'], hoja.get('total')
    ultima, contenido, merges, fila = _letra(len(columnas)), [], [], 1
    contenido.append(_fila_xlsx(fila, [(hoja['titulo'], estilo['titulo'], 'texto')], 26))
    merges.append(f'A{fila}:{ultima}{fila}')
    fila += 1
    for etiqueta, valor in hoja['filtros']:
        contenido.append(_fila_xlsx(fila, [(etiqueta, estilo['etiqueta'], 'texto'),
                                           (valor, estilo['valor'], 'texto')], 15))
        merges.append(f'B{fila}:{ultima}{fila}')
        fila += 1
    fila += 1
    cabecera = fila
    contenido.append(_fila_xlsx(fila, [(columna[0], estilo['encabezado'], 'texto') for columna in columnas], 30))
    fila += 1
    for registro in filas:
        contenido.append(_fila_xlsx(fila, [(valor, estilo[_estilo_dato(tipo)], tipo)
                                           for valor, (_, tipo, _) in zip(registro, columnas)], 15))
        fila += 1
    if total:
        contenido.append(_fila_xlsx(fila, [(valor, estilo[_estilo_total(tipo)], tipo)
                                           for valor, (_, tipo, _) in zip(total, columnas)], 18))
        fila += 1
    if hoja.get('nota'):
        contenido.append(_fila_xlsx(fila, [(hoja['nota'], estilo['nota'], 'texto')], 14))
        merges.append(f'A{fila}:{ultima}{fila}')
        fila += 1
    anchos = ''.join(f'<col min="{posicion}" max="{posicion}" width="{columna[2]}" customWidth="1"/>'
                     for posicion, columna in enumerate(columnas, 1))
    uniones = (f'<mergeCells count="{len(merges)}">'
               + ''.join(f'<mergeCell ref="{merge}"/>' for merge in merges) + '</mergeCells>') if merges else ''
    return (_XML + f'<worksheet xmlns="{_NS_MAIN}" xmlns:r="{_NS_REL}">'
            f'<dimension ref="A1:{ultima}{fila - 1}"/>'
            '<sheetViews><sheetView workbookViewId="0">'
            f'<pane xSplit="1" ySplit="{cabecera}" topLeftCell="B{cabecera + 1}" activePane="bottomRight" state="frozen"/>'
            '</sheetView></sheetViews><sheetFormatPr defaultRowHeight="15"/>'
            f'<cols>{anchos}</cols><sheetData>{"".join(contenido)}</sheetData>{uniones}'
            '<pageMargins left="0.5" right="0.5" top="0.6" bottom="0.6" header="0.3" footer="0.3"/>'
            '<pageSetup paperSize="9" orientation="landscape"/></worksheet>')


def _content_types(total):
    hojas = ''.join(f'<Override PartName="/xl/worksheets/sheet{numero}.xml" ContentType="application/'
                    'vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                    for numero in range(1, total + 1))
    return (_XML + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.'
            'spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.'
            f'spreadsheetml.styles+xml"/>{hojas}</Types>')


def _workbook_xml(nombres):
    hojas = ''.join(f'<sheet name="{escape(nombre)}" sheetId="{posicion}" r:id="rId{posicion}"/>'
                    for posicion, nombre in enumerate(nombres, 1))
    return _XML + f'<workbook xmlns="{_NS_MAIN}" xmlns:r="{_NS_REL}"><sheets>{hojas}</sheets></workbook>'


def _relaciones_libro(total):
    hojas = ''.join(f'<Relationship Id="rId{numero}" Type="{_NS_REL}/worksheet" '
                    f'Target="worksheets/sheet{numero}.xml"/>' for numero in range(1, total + 1))
    return (_XML + f'<Relationships xmlns="{_NS_PKG}">{hojas}'
            f'<Relationship Id="rId{total + 1}" Type="{_NS_REL}/styles" Target="styles.xml"/></Relationships>')


def excel(model):
    """Libro XLSX del reporte: hoja RESUMEN (matriz) y hojas de detalle.

    Se arma con ``zipfile`` y XML de OOXML (las dependencias que ya usaba el
    proyecto). Los importes quedan como números reales con formato monetario.
    """
    hojas = [dict(nombre='RESUMEN', titulo=model['titulo'], filtros=model['filtros'], columnas=model['columnas'],
                  filas=model['filas'], total=model.get('total'), nota=model.get('nota'))]
    hojas.extend(model.get('hojas') or [])
    stream = BytesIO()
    with ZipFile(stream, 'w', ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml', _content_types(len(hojas)))
        archive.writestr('_rels/.rels', _RELACIONES_RAIZ)
        archive.writestr('xl/workbook.xml', _workbook_xml([hoja['nombre'] for hoja in hojas]))
        archive.writestr('xl/_rels/workbook.xml.rels', _relaciones_libro(len(hojas)))
        archive.writestr('xl/styles.xml', _ESTILOS_XML)
        for posicion, hoja in enumerate(hojas, 1):
            archive.writestr(f'xl/worksheets/sheet{posicion}.xml', _hoja_xlsx(hoja, _INDICES_ESTILO))
    return stream.getvalue()


ANCHO_PAGINA, ALTO_PAGINA = 842, 595          # A4 horizontal, en puntos
MARGEN = 24.0
TAMANO_MAX, TAMANO_MIN = 9.0, 7.0             # la letra nunca baja de TAMANO_MIN
RELLENO = 3.0
ALTO_FILA = 15.0
ALTO_ENCABEZADO_PDF = 22.0
_AZUL = '0.12 0.22 0.39'
_AZUL_CLARO = '0.86 0.91 0.96'
_BORDE = '0.70 0.75 0.85'
# Anchos de Helvetica (por mil) de los caracteres que usa el reporte; el resto se
# estima en 620 para no quedarse corto al repartir las columnas.
_ANCHOS_HELVETICA = {ord(' '): 278, ord('!'): 278, ord('"'): 355, ord('%'): 889, ord('&'): 667,
                     ord("'"): 191, ord('('): 333, ord(')'): 333, ord('*'): 389, ord('+'): 584,
                     ord(','): 278, ord('-'): 333, ord('.'): 278, ord('/'): 278, ord(':'): 278,
                     ord(';'): 278, ord('<'): 584, ord('='): 584, ord('>'): 584, ord('?'): 556,
                     ord('@'): 1015, ord('['): 278, ord(']'): 278, ord('_'): 556, ord('Ñ'): 722}
for _digito in range(10):
    _ANCHOS_HELVETICA[ord(str(_digito))] = 556


def _ancho_texto(texto, tamano):
    """Ancho aproximado del texto en Helvetica (suficiente para alinear la tabla)."""
    return sum(_ANCHOS_HELVETICA.get(ord(caracter), 620) for caracter in str(texto)) * tamano / 1000


def _formato(valor, tipo):
    """Texto visible de una celda: los importes siempre con dos decimales.

    Acepta etiquetas de texto dentro de columnas numéricas (p. ej. "TOTAL GENERAL"
    en la columna del numero de devolucion) sin romper la exportacion.
    """
    if tipo == 'moneda' and isinstance(valor, (int, Decimal)) and not isinstance(valor, bool):
        return f'{_decimal(valor):,.2f}'
    if tipo == 'entero' and isinstance(valor, (int, Decimal)) and not isinstance(valor, bool):
        return str(int(valor))
    return str(valor if valor is not None else '')


def _ancho_columna(indice, columnas, filas, total, tamano):
    """Ancho necesario de una columna: el mayor entre sus valores y su encabezado."""
    etiqueta, tipo, _ancho = columnas[indice]
    valores = [_formato(fila[indice], tipo) for fila in filas]
    if total is not None:
        valores.append(_formato(total[indice], tipo))
    ancho_valor = max([_ancho_texto(valor, tamano) for valor in valores] or [0.0])
    ancho_etiqueta = max(_ancho_texto(palabra, tamano - 0.5) for palabra in etiqueta.split())
    return max(ancho_valor, ancho_etiqueta, {'moneda': 26.0, 'entero': 16.0}.get(tipo, 40.0)) + 2 * RELLENO


def _bloques(anchos, disponible):
    """Reparte las columnas en páginas: la primera (sucursal) se repite en cada una."""
    if sum(anchos) <= disponible:
        return [list(range(len(anchos)))]
    bloques, actual, usado = [], [0], anchos[0]
    for indice in range(1, len(anchos)):
        if usado + anchos[indice] > disponible:
            bloques.append(actual)
            actual, usado = [0], anchos[0]
        actual.append(indice)
        usado += anchos[indice]
    bloques.append(actual)
    return bloques


def _diseno(columnas, filas, total):
    """Tamaño de letra y reparto de columnas: primero se prueba una sola página."""
    disponible = ANCHO_PAGINA - 2 * MARGEN
    for tamano in (TAMANO_MAX, 8.5, 8.0, 7.5, TAMANO_MIN):
        anchos = [_ancho_columna(indice, columnas, filas, total, tamano) for indice in range(len(columnas))]
        if sum(anchos) <= disponible:
            return tamano, anchos, [list(range(len(columnas)))]
    anchos = [_ancho_columna(indice, columnas, filas, total, TAMANO_MIN) for indice in range(len(columnas))]
    return TAMANO_MIN, anchos, _bloques(anchos, disponible)


def _escapar(texto):
    return str(texto).replace('\\', r'\\').replace('(', r'\(').replace(')', r'\)')


def _texto(x, y, texto, fuente, tamano, blanco=False):
    color = '1 1 1 rg' if blanco else '0 0 0 rg'
    return f'BT {color} /{fuente} {tamano:.2f} Tf 1 0 0 1 {x:.2f} {y:.2f} Tm ({_escapar(texto)}) Tj ET'


def _partir(etiqueta, ancho, tamano):
    """Divide el encabezado de una columna en dos líneas como máximo."""
    lineas = ['']
    for palabra in etiqueta.split():
        candidato = f'{lineas[-1]} {palabra}'.strip()
        if lineas[-1] and len(lineas) < 2 and _ancho_texto(candidato, tamano) > ancho - 2 * RELLENO:
            lineas.append(palabra)
        else:
            lineas[-1] = candidato
    return [linea for linea in lineas if linea] or [etiqueta]


def _fila_pdf(columnas, anchos, bloque, valores, y, tamano, total_fila=False):
    """Dibuja una fila de la tabla con bordes, relleno de total y alineación numérica."""
    operaciones, x = [], MARGEN
    if total_fila:
        ancho_bloque = sum(anchos[indice] for indice in bloque)
        operaciones.append(f'{_AZUL_CLARO} rg {MARGEN:.2f} {y - ALTO_FILA:.2f} {ancho_bloque:.2f} {ALTO_FILA:.2f} re f')
    fuente = 'F2' if total_fila else 'F1'
    for indice in bloque:
        _, tipo, _ancho = columnas[indice]
        ancho = anchos[indice]
        operaciones.append(f'{_BORDE} RG 0.5 w {x:.2f} {y - ALTO_FILA:.2f} {ancho:.2f} {ALTO_FILA:.2f} re S')
        texto = _formato(valores[indice], tipo)
        base = y - ALTO_FILA + (ALTO_FILA - tamano) / 2 + 1.4
        if indice == bloque[0] and tipo == 'texto':
            operaciones.append(_texto(x + RELLENO, base, texto, fuente, tamano))
        else:
            derecha = x + ancho - RELLENO - _ancho_texto(texto, tamano)
            operaciones.append(_texto(derecha, base, texto, fuente, tamano))
        x += ancho
    return operaciones


def _pagina_pdf(model, tamano, anchos, bloque, filas, total_fila, numero, totales, continuacion):
    """Contenido de una página: encabezado (filtros), tabla de la matriz y pie."""
    columnas = model['columnas']
    operaciones, y = [], ALTO_PAGINA - MARGEN
    if continuacion:
        operaciones.append(_texto(MARGEN, y - 11, f"{model['titulo']} · continuación", 'F2', 11))
        y -= 18
        resumen = ' · '.join([f"Período: {model['filtros'][0][1]}"]
                             + [f'{etiqueta} {valor}' for etiqueta, valor in model['filtros'][1:4]])
        operaciones.append(_texto(MARGEN, y - 9, resumen, 'F1', 8.5))
        y -= 20
    else:
        operaciones.append(_texto((ANCHO_PAGINA - _ancho_texto(model['titulo'], 15)) / 2, y - 15,
                                  model['titulo'], 'F2', 15))
        y -= 30
        for etiqueta, valor in model['filtros']:
            operaciones.append(_texto(MARGEN, y - 10, etiqueta, 'F2', 9.5))
            operaciones.append(_texto(MARGEN + _ancho_texto(etiqueta, 9.5) + 4, y - 10, valor, 'F1', 9.5))
            y -= 14
        y -= 8
    x = MARGEN
    for indice in bloque:
        ancho = anchos[indice]
        operaciones.append(f'{_AZUL} rg {x:.2f} {y - ALTO_ENCABEZADO_PDF:.2f} {ancho:.2f} '
                           f'{ALTO_ENCABEZADO_PDF:.2f} re f')
        for posicion, linea in enumerate(_partir(columnas[indice][0], ancho, tamano - 0.5)):
            operaciones.append(_texto(x + (ancho - _ancho_texto(linea, tamano - 0.5)) / 2,
                                      y - 9.0 - posicion * 8.5, linea, 'F2', tamano - 0.5, blanco=True))
        x += ancho
    y -= ALTO_ENCABEZADO_PDF
    for fila in filas:
        operaciones += _fila_pdf(columnas, anchos, bloque, fila, y, tamano)
        y -= ALTO_FILA
    if total_fila is not None:
        operaciones += _fila_pdf(columnas, anchos, bloque, total_fila, y, tamano, total_fila=True)
    if model.get('nota'):
        operaciones.append(_texto(MARGEN, MARGEN + 14, model['nota'], 'F3', 7))
    pie = f'Página {numero} de {totales}'
    operaciones.append(_texto(ANCHO_PAGINA - MARGEN - _ancho_texto(pie, 7.5), MARGEN, pie, 'F1', 7.5))
    return '\n'.join(operaciones)


def _paginas_pdf(model):
    """Reparte la matriz en páginas: columnas por ancho y filas por alto."""
    columnas, filas, total = model['columnas'], model['filas'], model.get('total')
    tamano, anchos, bloques = _diseno(columnas, filas, total)
    alto_cabecera = 30 + 14 * len(model['filtros']) + 8
    capacidad = int((ALTO_PAGINA - 2 * MARGEN - alto_cabecera - ALTO_ENCABEZADO_PDF - 24) / ALTO_FILA)
    por_pagina = max(1, capacidad)
    paginas = []
    for bloque in bloques:
        for inicio in (range(0, len(filas), por_pagina) if filas else [0]):
            ultimo = not filas or inicio + por_pagina >= len(filas)
            paginas.append(dict(bloque=bloque, filas=filas[inicio:inicio + por_pagina],
                                total=total if (ultimo and total is not None) else None))
    return tamano, anchos, paginas


def _documento(paginas):
    """Ensambla el PDF 1.4 con las fuentes base de Helvetica (sin dependencias extra)."""
    objetos = [b'<< /Type /Catalog /Pages 2 0 R >>', b'',
               b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>',
               b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>',
               b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Oblique /Encoding /WinAnsiEncoding >>']
    referencias = []
    for contenido in paginas:
        identificador = len(objetos) + 1
        referencias.append(f'{identificador} 0 R')
        objetos.append((f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {ANCHO_PAGINA} {ALTO_PAGINA}] /Resources '
                        '<< /Font << /F1 3 0 R /F2 4 0 R /F3 5 0 R >> >> /Contents '
                        f'{identificador + 1} 0 R >>').encode('cp1252', errors='replace'))
        codificado = contenido.encode('cp1252', errors='replace')
        objetos.append(b'<< /Length ' + str(len(codificado)).encode()
                       + b' >>\nstream\n' + codificado + b'\nendstream')
    objetos[1] = f'<< /Type /Pages /Count {len(paginas)} /Kids [{" ".join(referencias)}] >>'.encode()
    salida, posiciones = bytearray(b'%PDF-1.4\n'), []
    for numero, objeto in enumerate(objetos, 1):
        posiciones.append(len(salida))
        salida += f'{numero} 0 obj\n'.encode() + objeto + b'\nendobj\n'
    inicio = len(salida)
    salida += f'xref\n0 {len(objetos) + 1}\n'.encode() + b'0000000000 65535 f \n'
    for posicion in posiciones:
        salida += f'{posicion:010d} 00000 n \n'.encode()
    salida += (f'trailer\n<< /Size {len(objetos) + 1} /Root 1 0 R >>\nstartxref\n{inicio}\n%%EOF\n').encode()
    return bytes(salida)


def pdf(model):
    """PDF horizontal (A4 apaisado) del reporte con encabezados repetidos y total destacado."""
    tamano, anchos, paginas = _paginas_pdf(model)
    streams = [_pagina_pdf(model, tamano, anchos, pagina['bloque'], pagina['filas'], pagina['total'],
                           numero, len(paginas), continuacion=numero > 1)
               for numero, pagina in enumerate(paginas, 1)]
    return _documento(streams)


