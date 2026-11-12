"""Servicio de notificaciones push y alertas en tiempo real para clientes y empresas."""
import asyncio
import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from threading import Lock
from typing import Any
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.modules.cliente_experiencia_compra.shared.models.catalogo import Producto, VarianteProd

logger = logging.getLogger(__name__)

# Almacenamiento en memoria volátil de notificaciones por empresa (máximo 50 por tenant)
_lock = Lock()
_tenant_notifications: dict[int, list[dict[str, Any]]] = {}
_tenant_subscribers: dict[int, set[asyncio.Queue]] = {}


def add_notification(idemp: int, notification: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        if idemp not in _tenant_notifications:
            _tenant_notifications[idemp] = []
        _tenant_notifications[idemp].insert(0, notification)
        # Mantener últimas 50 notificaciones por empresa
        if len(_tenant_notifications[idemp]) > 50:
            _tenant_notifications[idemp] = _tenant_notifications[idemp][:50]
        queues = list(_tenant_subscribers.get(idemp, set()))

    # Notificar a los suscriptores SSE activos
    for q in queues:
        try:
            q.put_nowait(notification)
        except Exception:
            pass
    return notification


def get_notifications(idemp: int, limit: int = 20) -> list[dict[str, Any]]:
    with _lock:
        items = _tenant_notifications.get(idemp, [])
        return list(items[:limit])


def subscribe_stream(idemp: int) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue()
    with _lock:
        if idemp not in _tenant_subscribers:
            _tenant_subscribers[idemp] = set()
        _tenant_subscribers[idemp].add(q)
    return q


def unsubscribe_stream(idemp: int, q: asyncio.Queue) -> None:
    with _lock:
        subscribers = _tenant_subscribers.get(idemp)
        if subscribers and q in subscribers:
            subscribers.remove(q)
            if not subscribers:
                _tenant_subscribers.pop(idemp, None)


def notify_promotion_launched(
    db: Session,
    idemp: int,
    promo_id: int,
    nombre: str,
    descripcion: str,
    tipo_descuento: str,
    valor_descuento: Decimal,
    fecha_ini: date,
    fecha_fin: date,
    product_ids: list[str],
    delivery=None,
) -> dict[str, Any]:
    """Crea y despacha una notificación push a los clientes registrados de la empresa al lanzar una promoción.

    Regla estricta: NO se incluye ningún ID interno de base de datos (idprod, idvariante).
    Solo se muestra el nombre de la prenda y/o su SKU.
    """
    empresa_nombre = "FashionStore"
    try:
        emp_row = db.execute(
            text("SELECT nombre FROM public.empresa WHERE idemp = :idemp"),
            {"idemp": idemp},
        ).mappings().first()
        if emp_row and emp_row.get("nombre"):
            empresa_nombre = emp_row["nombre"]
    except Exception:
        pass

    # Consultar nombres y SKUs de las prendas en promoción (sin IDs de BD)
    garments: dict[str, list[str]] = {}
    try:
        rows = db.execute(
            select(Producto.descripcion, VarianteProd.sku)
            .outerjoin(VarianteProd, (VarianteProd.idprod == Producto.idprod) & (VarianteProd.idemp == idemp))
            .where(Producto.idprod.in_(product_ids), Producto.idemp == idemp)
            .order_by(Producto.descripcion, VarianteProd.sku)
        ).all()
        for desc, sku in rows:
            if not desc:
                continue
            garments.setdefault(desc, [])
            if sku and sku not in garments[desc]:
                garments[desc].append(sku)
    except Exception as e:
        logger.warning("No se pudieron consultar detalles de variantes para notificación: %s", e)

    # Formatear el listado legible de prendas
    if garments:
        garment_parts = []
        for desc, skus in garments.items():
            if skus:
                garment_parts.append(f"{desc} (SKU: {', '.join(skus)})")
            else:
                garment_parts.append(desc)
        prendas_txt = "; ".join(garment_parts)
    else:
        prendas_txt = "Prendas seleccionadas del catálogo"

    descuento_txt = (
        f"{valor_descuento}%"
        if tipo_descuento == "porcentaje"
        else f"USD {valor_descuento}"
    )

    titulo = f"¡Nueva promoción en {empresa_nombre}!"
    resumen = (
        f"{nombre}: {descuento_txt} de descuento en {prendas_txt}. "
        f"Válido del {fecha_ini.strftime('%d/%m/%Y')} al {fecha_fin.strftime('%d/%m/%Y')}."
    )

    notification = {
        "id": str(uuid4()),
        "idEmp": idemp,
        "tipo": "promocion",
        "idPromo": promo_id,
        "titulo": titulo,
        "promocion": nombre,
        "descripcion": descripcion,
        "descuento": descuento_txt,
        "prendas": prendas_txt,
        "mensaje": resumen,
        "fechaInicio": fecha_ini.isoformat(),
        "fechaFin": fecha_fin.isoformat(),
        "creadoEn": datetime.now(timezone.utc).isoformat(),
    }

    # Guardar en memoria y notificar a clientes conectados a la tienda
    add_notification(idemp, notification)

    # Buscar clientes registrados de la empresa para enviar notificación push / email
    clientes = []
    try:
        clientes = db.execute(
            text("""
                SELECT u.idusuario, u.nombre, u.correo
                FROM public.cliente c
                JOIN public.usuario u ON u.idusuario = c.idusuario
                WHERE u.idemp = :idemp AND u.tipo = 'C' AND u.correo IS NOT NULL
            """),
            {"idemp": idemp},
        ).mappings().all()
    except Exception as e:
        logger.warning("No se pudieron consultar clientes registrados para notificación: %s", e)

    # Despachar por correo a clientes registrados si el servicio de correo está disponible
    if delivery and hasattr(delivery, "dispatch_email") and clientes:
        asunto = f"[Promoción] {nombre} — {empresa_nombre}"
        cuerpo = (
            f"Hola,\n\n"
            f"{empresa_nombre} ha lanzado una nueva promoción para ti:\n\n"
            f"Promoción: {nombre}\n"
            f"Descuento: {descuento_txt}\n"
            f"Prendas incluidas: {prendas_txt}\n"
            f"Vigencia: Del {fecha_ini.strftime('%d/%m/%Y')} al {fecha_fin.strftime('%d/%m/%Y')}\n\n"
            f"Detalles:\n{descripcion}\n\n"
            f"Ingresa a la tienda en línea para aprovechar este descuento antes de que finalice.\n"
        )
        for cl in clientes:
            correo = cl.get("correo")
            if correo:
                try:
                    delivery.dispatch_email(correo, asunto, cuerpo)
                except Exception as ex:
                    logger.warning("Error al despachar correo a cliente %s: %s", correo, ex)

    logger.info(
        "Notificación de promoción %s emitida para empresa %s (%d clientes notificados)",
        promo_id,
        idemp,
        len(clientes),
    )
    return notification
