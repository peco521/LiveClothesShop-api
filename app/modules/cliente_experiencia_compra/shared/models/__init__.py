"""Modelos compartidos del módulo cliente_experiencia_compra.

Mapean únicamente tablas del schema oficial database/schema.sql,
en el mismo orden de columnas. No duplican Usuario, Cliente, Sesion,
Ciudad ni Sucursal (se reutilizan desde seguridad_accesos).
"""
from app.modules.cliente_experiencia_compra.shared.models.catalogo import (
    Categoria,
    Coleccion,
    Color,
    Inventario,
    Marca,
    Producto,
    Promocion,
    Proveedor,
    Talla,
    TempColeccion,
    Temporada,
    VarianteColor,
    VarianteProd,
)
from app.modules.cliente_experiencia_compra.shared.models.comercio import (
    Carrito,
    DetalleCarro,
    DetalleReserva,
    DetalleVenta,
    HorarioAtencion,
    HorarioSuc,
    MovimientoInv,
    Pago,
    Reserva,
    Venta,
)

__all__ = ["Categoria", "Coleccion", "Color", "Inventario", "Marca", "Producto",
           "Promocion", "Proveedor", "Talla", "TempColeccion", "Temporada",
           "VarianteColor", "VarianteProd", "Carrito", "DetalleCarro",
           "DetalleReserva", "DetalleVenta", "HorarioAtencion",
           "HorarioSuc", "MovimientoInv", "Pago", "Reserva", "Venta"]
