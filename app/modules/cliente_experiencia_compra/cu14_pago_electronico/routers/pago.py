from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.modules.cliente_experiencia_compra.cu14_pago_electronico.schemas.pago import (
    PagoCrear,
    PagoDetalle,
    PagoReprocesar,
)
from app.modules.cliente_experiencia_compra.cu14_pago_electronico.services import pago as service
from app.modules.cliente_experiencia_compra.shared.dependencies import require_cliente
from app.core.errors import DomainError
from app.modules.cliente_experiencia_compra.cu14_pago_electronico.services import checkout_stripe

router = APIRouter(prefix="/api/cliente/pagos", tags=["Pago electrónico"],
                   dependencies=[Depends(require_cliente)])


def peer(request):
    return request.client.host if request.client else None


def pasarela(request):
    return request.app.state.pasarela


def environment(request):
    return request.app.state.settings.environment


@router.get("/configuracion")
def configuration(request: Request):
    settings = request.app.state.settings
    return {"proveedor": settings.payments_provider,
            "simulacion": settings.payments_provider == "mock" and settings.environment != "production",
            "disponible": settings.payments_provider == "stripe" or settings.environment != "production",
            "moneda": settings.stripe_currency if settings.payments_provider == "stripe" else None}


def checked_provider(request):
    if request.app.state.settings.payments_provider == "mock" and environment(request) == "production":
        raise DomainError(503, "pasarela_no_disponible", "Configure una pasarela de pago; la simulación está deshabilitada")
    return getattr(request.app.state, "stripe", None)


@router.post("", response_model=PagoDetalle, status_code=201)
def pay(data: PagoCrear, request: Request, response: Response,
        db: Session = Depends(get_db), identity=Depends(require_cliente)):
    stripe = checked_provider(request)
    try:
        if stripe:
            vista, creado = checkout_stripe.start(db, data, identity.usuario.idUsuario, peer(request), stripe)
        else:
            vista, creado = service.iniciar(db, data, identity.usuario.idUsuario, peer(request),
                                            pasarela(request), environment(request))
    except RuntimeError:
        raise DomainError(503, "pasarela_no_disponible", "No se pudo comunicar con la pasarela; consulta el estado") from None
    # Idempotencia: pago existente reutilizado se devuelve con 200.
    if not creado:
        response.status_code = 200
    return vista


@router.get("/{idPago}", response_model=PagoDetalle)
def detail(idPago: int, request: Request, db: Session = Depends(get_db),
           identity=Depends(require_cliente)):
    return service.consultar(db, idPago, identity.usuario.idUsuario)


@router.post("/{idPago}/procesar", response_model=PagoDetalle)
def reprocess(idPago: int, data: PagoReprocesar, request: Request,
              db: Session = Depends(get_db), identity=Depends(require_cliente)):
    if checked_provider(request):
        raise DomainError(409, "reintento_no_permitido", "Utilice reconciliar para consultar Stripe sin crear otro cobro")
    vista, _ = service.reprocesar(db, idPago, data, identity.usuario.idUsuario,
                                  peer(request), pasarela(request), environment(request))
    return vista


@router.post("/{idPago}/reconciliar", response_model=PagoDetalle)
def reconcile(idPago: int, request: Request, db: Session = Depends(get_db), identity=Depends(require_cliente)):
    stripe = checked_provider(request)
    if stripe is None:
        return service.consultar(db, idPago, identity.usuario.idUsuario)
    try:
        return checkout_stripe.reconcile(db, idPago, identity.usuario.idUsuario, peer(request), stripe)
    except RuntimeError:
        raise DomainError(503, "pasarela_no_disponible", "No se pudo consultar Stripe") from None
