"""Router de notificaciones push para clientes registrados de la empresa."""
import asyncio
import json
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.notifications import get_notifications, subscribe_stream, unsubscribe_stream
from app.modules.inventario_productos.shared.tenant import tenant_id

router = APIRouter(prefix="/api/cliente/notificaciones", tags=["Notificaciones Push Cliente"])


@router.get("")
def listar_notificaciones(db: Session = Depends(get_db)):
    """Obtiene las notificaciones recientes para la empresa del cliente en sesión."""
    empresa_id = tenant_id(db) or 1
    return {"items": get_notifications(empresa_id, limit=20)}


@router.get("/stream")
async def stream_notificaciones(request: Request, db: Session = Depends(get_db)):
    """Canal Server-Sent Events (SSE) para recibir notificaciones push de promociones en tiempo real."""
    empresa_id = tenant_id(db) or 1
    queue = subscribe_stream(empresa_id)

    async def event_generator():
        try:
            # Enviar mensaje inicial de conexión
            yield f"data: {json.dumps({'tipo': 'conectado', 'idEmp': empresa_id})}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    # Esperar notificación con timeout de keepalive (15s)
                    notification = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {json.dumps(notification)}\n\n"
                except asyncio.TimeoutError:
                    # Keepalive ping
                    yield ": keepalive\n\n"
        finally:
            unsubscribe_stream(empresa_id, queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
