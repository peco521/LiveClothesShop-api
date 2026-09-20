"""Consultas de CU17 (solo lectura, agrupadas para evitar N+1).

Fuentes reales del esquema existente: ``detalleventa`` + ``pago`` (compras
pagadas), ``detallereserva`` (reservas vigentes) y ``detallecarro`` (carrito
activo). Las tres terminan en ``varianteprod`` -> ``producto``.

El candidato se obtiene del repositorio compartido de CU10 para no redefinir
"producto disponible": no se crea una segunda regla de disponibilidad.
"""

from sqlalchemy import exists, func, select

from app.modules.cliente_experiencia_compra.shared.models.catalogo import (
    Producto,
    TempColeccion,
    Temporada,
    VarianteProd,
)
from app.modules.cliente_experiencia_compra.shared.models.comercio import (
    Carrito,
    DetalleCarro,
    DetalleReserva,
    DetalleVenta,
    Pago,
    Reserva,
    Venta,
)
from app.modules.cliente_experiencia_compra.shared.repositories import catalogo as catalogo_repository

# Estados de reserva que expresan interés real. Canceladas y vencidas no aportan
# señal al perfil porque el cliente renunció a lo que había reservado.
ESTADOS_RESERVA_INTERES = ("pendiente", "confirmada", "atendida")


def _pago_aprobado():
    """Una venta cuenta como pagada si tiene un pago aprobado (CU13/CU14/CU25)."""
    return exists(select(Pago.idpago).where(Pago.nroventa == Venta.nroventa,
                                            Pago.estado == "aprobado"))


def compras_pagadas(db, user_id):
    """Líneas de compras propias pagadas (la señal más fuerte del perfil)."""
    return db.execute(
        select(Producto.idprod, Producto.idcat, Producto.idmarca, Producto.idcol,
               DetalleVenta.cantidad.label("cantidad"),
               DetalleVenta.preciounitario.label("precio"))
        .join(VarianteProd, VarianteProd.idvariante == DetalleVenta.idvar)
        .join(Producto, Producto.idprod == VarianteProd.idprod)
        .join(Venta, Venta.nroventa == DetalleVenta.nroventa)
        .where(Venta.idusuariocl == user_id,
               Venta.estado != "anulada",
               _pago_aprobado())
    ).all()


def reservas_vigentes(db, user_id):
    """Líneas de reservas propias vigentes (interés declarado por el cliente)."""
    return db.execute(
        select(Producto.idprod, Producto.idcat, Producto.idmarca, Producto.idcol,
               DetalleReserva.cantidad.label("cantidad"),
               VarianteProd.precio.label("precio"))
        .join(VarianteProd, VarianteProd.idvariante == DetalleReserva.idvar)
        .join(Producto, Producto.idprod == VarianteProd.idprod)
        .join(Reserva, Reserva.nroreserva == DetalleReserva.nroreserva)
        .where(Reserva.idusuariocl == user_id,
               Reserva.estado.in_(ESTADOS_RESERVA_INTERES))
    ).all()


def carrito_activo(db, user_id):
    """Líneas del carrito activo propio (interés inmediato, solo lectura)."""
    return db.execute(
        select(Producto.idprod, Producto.idcat, Producto.idmarca, Producto.idcol,
               DetalleCarro.cantidad.label("cantidad"),
               VarianteProd.precio.label("precio"))
        .join(VarianteProd, VarianteProd.idvariante == DetalleCarro.idvar)
        .join(Producto, Producto.idprod == VarianteProd.idprod)
        .join(Carrito, Carrito.idcarrito == DetalleCarro.idcarrito)
        .where(Carrito.idusuariocl == user_id, Carrito.estado == "activo")
    ).all()


def temporadas_por_coleccion(db, colecciones):
    """Temporadas vinculadas a cada colección (tabla puente ``tempcoleccion``)."""
    ids = [idcol for idcol in set(colecciones) if idcol is not None]
    if not ids:
        return {}
    rows = db.execute(
        select(TempColeccion.idcol, Temporada.idtemp, Temporada.nombre)
        .join(Temporada, Temporada.idtemp == TempColeccion.idtemp)
        .where(TempColeccion.idcol.in_(ids))
    ).all()
    agrupadas: dict[int, list[tuple[int, str]]] = {}
    for idcol, idtemp, nombre in rows:
        agrupadas.setdefault(idcol, []).append((idtemp, nombre))
    return agrupadas


def candidatos_disponibles(db, limite):
    """Poleras ofrecibles reutilizando la regla de disponibilidad de CU10."""
    rows, _total = catalogo_repository.list_products(db, soloDisponibles=True,
                                                     limit=limite, sort="nombre_asc")
    return rows


def variantes_activas(db, product_ids):
    """Variantes activas por producto (un solo lote, mismas reglas que CU10)."""
    return catalogo_repository.product_variants(db, product_ids)


def disponibilidad_por_producto(db, product_ids):
    """Disponible por producto con la regla exacta de CU10 (cantDisp en sucursal activa)."""
    return catalogo_repository.availability_totals(db, product_ids)


def popularidad(db, product_ids):
    """Unidades pagadas por producto en una única consulta agrupada."""
    if not product_ids:
        return {}
    rows = db.execute(
        select(VarianteProd.idprod, func.coalesce(func.sum(DetalleVenta.cantidad), 0))
        .join(DetalleVenta, DetalleVenta.idvar == VarianteProd.idvariante)
        .join(Venta, Venta.nroventa == DetalleVenta.nroventa)
        .where(VarianteProd.idprod.in_(product_ids),
               Venta.estado != "anulada",
               _pago_aprobado())
        .group_by(VarianteProd.idprod)
    ).all()
    return {idprod: int(total or 0) for idprod, total in rows}
