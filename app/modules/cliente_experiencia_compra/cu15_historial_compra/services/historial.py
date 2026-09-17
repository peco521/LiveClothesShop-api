"""CU15 - Proyección de compras propias, sin modificar información histórica."""

from collections import defaultdict
from decimal import Decimal

from sqlalchemy.exc import SQLAlchemyError

from app.core.errors import DomainError
from app.modules.cliente_experiencia_compra.cu15_historial_compra.repositories import historial as repository
from app.modules.cliente_experiencia_compra.cu15_historial_compra.schemas.historial import (
    CompraDetalle,
    CompraItemDetalle,
    CompraPago,
    CompraResumen,
    CompraSucursal,
    HistorialCompras,
    ProductoCompraResumen,
)

MENSAJE_SIN_COMPRAS = "No existen compras en tu historial"


def _pago_principal(pagos):
    """Una aprobación prevalece; en otro caso se muestra el intento más reciente."""
    if not pagos:
        return None
    aprobados = [pago for pago in pagos if pago.estado == "aprobado"]
    return max(aprobados or pagos, key=lambda pago: pago.idpago)


def _agrupar_detalles(rows):
    agrupados = defaultdict(list)
    for row in rows:
        agrupados[row.nroventa].append(row)
    return agrupados


def _agrupar_pagos(rows):
    agrupados = defaultdict(list)
    for row in rows:
        agrupados[row.nroventa].append(row)
    return agrupados


def _producto(row):
    return dict(
        idVar=row.idvar,
        sku=row.sku or row.idvar,
        producto=row.producto or row.idvar,
        cantidad=row.cantidad,
    )


def listar(db, user_id: str, offset: int, limit: int, *, branch_id=None):
    try:
        ventas, total = repository.listar_ventas(db, user_id, offset, limit, **({"branch_id": branch_id} if branch_id is not None else {}))
        numeros = [venta.nroventa for venta in ventas]
        detalles = _agrupar_detalles(repository.detalles_de_ventas(db, numeros))
        pagos = _agrupar_pagos(repository.pagos_de_ventas(db, numeros))
    except SQLAlchemyError:
        raise DomainError(
            503,
            "historial_no_disponible",
            "No fue posible obtener el historial de compras",
        ) from None

    items = []
    for venta in ventas:
        pago = _pago_principal(pagos[venta.nroventa])
        items.append(CompraResumen(
            nroVenta=venta.nroventa,
            fechaHora=venta.fechahora,
            productos=[ProductoCompraResumen(**_producto(row))
                       for row in detalles[venta.nroventa]],
            monto=venta.total,
            estado=venta.estado,
            estadoPago=pago.estado if pago else None,
        ))
    return HistorialCompras(
        items=items,
        total=total,
        offset=offset,
        limit=limit,
        mensaje=MENSAJE_SIN_COMPRAS if total == 0 else None,
    )


def detalle(db, nro_venta: int, user_id: str, *, branch_id=None):
    try:
        venta = repository.venta_propia(db, nro_venta, user_id, **({"branch_id": branch_id} if branch_id is not None else {}))
        if venta is None:
            raise DomainError(404, "compra_no_encontrada", "Compra no encontrada")
        filas = repository.detalles_de_ventas(db, [venta.nroventa])
        pago = _pago_principal(repository.pagos_de_ventas(db, [venta.nroventa]))
        sedes = repository.sucursales(db, [venta.nrosuc])
    except DomainError:
        raise
    except SQLAlchemyError:
        raise DomainError(
            503,
            "historial_no_disponible",
            "No fue posible obtener el historial de compras",
        ) from None

    sucursal, ciudad = sedes.get(venta.nrosuc, (None, None))
    items = [CompraItemDetalle(
        **_producto(row),
        idDetalleVenta=row.iddetalleventa,
        precioUnitario=row.preciounitario,
        subtotalBruto=row.preciounitario * row.cantidad,
    ) for row in filas]
    bruto = sum((item.subtotalBruto for item in items), Decimal("0"))
    vista_pago = CompraPago(
        idPago=pago.idpago,
        metodo=pago.metodo,
        monto=pago.monto,
        estado=pago.estado,
        fechaHora=pago.fechahora,
        referencia=pago.referencia,
    ) if pago else None
    return CompraDetalle(
        nroVenta=venta.nroventa,
        fechaHora=venta.fechahora,
        estado=venta.estado,
        estadoPago=pago.estado if pago else None,
        nit=venta.nit,
        sucursal=CompraSucursal(
            nro=venta.nrosuc,
            nombre=sucursal.nombre if sucursal else str(venta.nrosuc),
            ciudad=ciudad.nombre if ciudad else "",
        ),
        idCarrito=venta.idcarrito,
        nroReserva=venta.nroreserva,
        items=items,
        brutoTotal=bruto,
        descAplicado=venta.desc_aplicado,
        total=venta.total,
        pago=vista_pago,
    )
