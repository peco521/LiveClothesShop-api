from datetime import datetime, time, timedelta
from decimal import Decimal
from io import BytesIO
from zipfile import ZipFile, ZIP_DEFLATED
from xml.sax.saxutils import escape
from sqlalchemy import func, select
from app.core.errors import DomainError
from app.modules.cliente_experiencia_compra.shared.models.comercio import Venta, Pago, DetalleVenta, Reserva, Carrito
from app.modules.cliente_experiencia_compra.shared.models.catalogo import Producto, VarianteProd, Inventario, TempColeccion
from app.modules.inventario_productos.cu23_gestionar_devoluciones.models.devoluciones import Devolucion, Reembolso
from app.modules.seguridad_accesos.shared.models import Sucursal


def dashboard(db, ini, fin, suc=None, cat=None, temp=None):
    if fin < ini:
        raise DomainError(422, 'rango_invalido', 'La fecha final no puede ser anterior a la inicial')
    start, end = datetime.combine(ini, time.min), datetime.combine(fin + timedelta(days=1), time.min)
    products = select(Producto.idprod)
    if cat is not None:
        products = products.where(Producto.idcat == cat)
    if temp is not None:
        products = products.where(select(TempColeccion.idcol).where(TempColeccion.idcol == Producto.idcol, TempColeccion.idtemp == temp).exists())
    # Los ids de variante se materializan: las consultas externas ya incluyen
    # producto/varianteprod y la autocorrelación dejaría las subconsultas sin FROM.
    variant_ids = list(db.scalars(select(VarianteProd.idvariante).where(VarianteProd.idprod.in_(products))))
    scope = [Venta.fechahora >= start, Venta.fechahora < end, Venta.estado == 'registrada', select(Pago.idpago).where(Pago.nroventa == Venta.nroventa, Pago.estado == 'aprobado').exists()]
    if suc is not None:
        scope.append(Venta.nrosuc == suc)
    if cat is not None or temp is not None:
        # Solo Venta se correlaciona: DetalleVenta debe permanecer en el FROM
        # aunque la consulta externa (p. ej. el ranking de más vendidos) ya la incluya.
        scope.append(select(DetalleVenta.nroventa).where(DetalleVenta.nroventa == Venta.nroventa, DetalleVenta.idvar.in_(variant_ids)).correlate(Venta).exists())
    by_branch = db.execute(select(Sucursal.nombre, func.count(Venta.nroventa), func.sum(Venta.total)).join(Venta, Venta.nrosuc == Sucursal.nro).where(*scope).group_by(Sucursal.nro, Sucursal.nombre).order_by(Sucursal.nombre)).all()
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
    returns_query = select(func.count()).select_from(Devolucion).join(Venta, Venta.nroventa == Devolucion.nroventa).where(Devolucion.fechahora >= start, Devolucion.fechahora < end)
    if suc is not None:
        returns_query = returns_query.where(Venta.nrosuc == suc)
    if cat is not None or temp is not None:
        from app.modules.inventario_productos.cu23_gestionar_devoluciones.models.devoluciones import DetalleDev
        returns_query = returns_query.where(select(DetalleDev.nrodev).join(DetalleVenta, (DetalleVenta.nroventa == DetalleDev.nroventa) & (DetalleVenta.iddetalleventa == DetalleDev.iddetalleventa)).where(DetalleDev.nrodev == Devolucion.nrodev, DetalleVenta.idvar.in_(variant_ids)).exists())
    return dict(ventas=sum(r[1] for r in by_branch), ingresos=sum((r[2] for r in by_branch), Decimal('0')), sucursales=[dict(nombre=r[0], ventas=r[1], total=r[2]) for r in by_branch], productos=[dict(nombre=r[0], unidades=r[1]) for r in sold], inventario=dict(stock=inventory[0], disponible=inventory[1], reservado=inventory[0]-inventory[1], agotados=inventory[2]), reservas=reservations, conversionReservas=round(100*converted/reservations, 2) if reservations else 0, carritos=carts, conversionCarritos=round(100*closed/carts, 2) if carts else 0, devoluciones=db.scalar(returns_query), nota='Ingresos de ventas pagadas que contienen productos del filtro; inventario al momento de consultar. Carritos por sucursal: solo carritos vinculados a ventas de esa sucursal.')


def rows_for_report(db, kind, ini, fin, suc=None, cat=None, temp=None):
    data = dashboard(db, ini, fin, suc, cat, temp)
    if kind == 'ventas':
        return [['Sucursal', 'Ventas pagadas', 'Importe']] + [[r['nombre'], r['ventas'], r['total']] for r in data['sucursales']]
    if kind == 'inventario':
        return [['Indicador', 'Unidades']] + [[k, v] for k, v in data['inventario'].items()]
    if kind == 'reservas':
        return [['Indicador', 'Valor'], ['Reservas', data['reservas']], ['Conversion a venta (%)', data['conversionReservas']]]
    return [['Indicador', 'Valor'], ['Devoluciones registradas', data['devoluciones']]]


def excel(rows):
    def letters(n):
        result = ''
        while n:
            n, digit = divmod(n-1, 26); result = chr(65+digit)+result
        return result
    content = ''.join('<row r="%d">%s</row>' % (i, ''.join('<c r="%s%d" t="inlineStr"><is><t>%s</t></is></c>' % (letters(j), i, escape(str(v))) for j, v in enumerate(row, 1))) for i, row in enumerate(rows, 1))
    stream = BytesIO()
    with ZipFile(stream, 'w', ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml', '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
        archive.writestr('_rels/.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        archive.writestr('xl/workbook.xml', '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Reporte" sheetId="1" r:id="rId1"/></sheets></workbook>')
        archive.writestr('xl/_rels/workbook.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        archive.writestr('xl/worksheets/sheet1.xml', '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'+content+'</sheetData></worksheet>')
    return stream.getvalue()


def pdf(rows):
    lines = [' | '.join(str(v) for v in row) for row in rows]
    pages = [lines[i:i+40] for i in range(0, len(lines), 40)] or [[]]
    objects = [b'', b'', b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>']
    kids = []
    for page in pages:
        page_id = len(objects)+1; stream_id = page_id+1; kids.append(f'{page_id} 0 R')
        objects.append(f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 3 0 R >> >> /Contents {stream_id} 0 R >>'.encode())
        content = 'BT /F1 11 Tf 40 800 Td 18 TL\n'
        for line in page:
            safe = line.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)').replace('\n', ' ').replace('\r', ' ')
            content += f'({safe}) Tj T*\n'
        encoded = (content+'ET').encode('cp1252', errors='replace')
        objects.append(f'<< /Length {len(encoded)} >>\nstream\n'.encode()+encoded+b'\nendstream')
    objects[0] = b'<< /Type /Catalog /Pages 2 0 R >>'
    objects[1] = f'<< /Type /Pages /Count {len(pages)} /Kids [{" ".join(kids)}] >>'.encode()
    result = bytearray(b'%PDF-1.4\n'); offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(result)); result.extend(f'{i} 0 obj\n'.encode()+obj+b'\nendobj\n')
    xref = len(result); result.extend(f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode())
    for offset in offsets[1:]: result.extend(f'{offset:010d} 00000 n \n'.encode())
    result.extend(f'trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF'.encode())
    return bytes(result)
