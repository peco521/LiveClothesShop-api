from contextlib import asynccontextmanager
import asyncio
import logging

from fastapi import FastAPI, Request, Response
from pydantic import ValidationError

from app.core.config import Settings
from app.core.access_tokens import AccessTokens
from app.core.database import create_database
from app.core.errors import error_response, register_handlers
from app.core.security import Passwords
from app.modules.seguridad_accesos.routers.auth import router
from app.modules.seguridad_accesos.services.recovery_delivery import RecoveryDelivery
from app.modules.seguridad_accesos.cu05_usuarios_empleados.routers.usuario import router as cu05_router
from app.modules.seguridad_accesos.cu06_roles_permisos.routers.rol import router as cu06_router
from app.modules.seguridad_accesos.cu07_clientes.routers.cliente import router as cu07_router
from app.modules.seguridad_accesos.cu08_bitacora.routers.bitacora import router as cu08_router
from app.modules.seguridad_accesos.cu09_sucursales_ciudades.routers.organizacion import router as cu09_router
from app.modules.cliente_experiencia_compra.cu10_consultar_prendas.routers.catalogo import router as cu10_router
from app.modules.cliente_experiencia_compra.cu11_gestionar_reserva.routers.reserva import router as cu11_router
from app.modules.cliente_experiencia_compra.cu11_gestionar_reserva.routers.sucursales import router as cu11_sucursales_router
from app.modules.cliente_experiencia_compra.cu12_carrito.routers.carrito import router as cu12_router
from app.modules.cliente_experiencia_compra.cu13_compra_digital.routers.compra import router as cu13_router
from app.modules.cliente_experiencia_compra.cu14_pago_electronico.routers.pago import router as cu14_router
from app.modules.cliente_experiencia_compra.cu15_historial_compra.routers.historial import router as cu15_router
from app.modules.cliente_experiencia_compra.cu15_historial_compra.routers.administracion import router as cu15_admin_router
from app.integrations.payments.mock import PasarelaMock
from app.modules.inventario_productos.cu18_gestionar_catalogo.routers.catalogo import router as cu18_router
from app.modules.inventario_productos.cu19_gestionar_proveedores.routers.proveedor import router as cu19_router
from app.modules.inventario_productos.cu20_gestionar_inventario.routers.inventario import router as cu20_router
from app.integrations.payments.protocolo import PasarelaPagos
from app.integrations.gmail import configured_gmail_delivery
from app.integrations.payments.stripe_checkout import StripeCheckout
from app.modules.cliente_experiencia_compra.cu14_pago_electronico.routers.stripe import router as stripe_router
from app.modules.inventario_productos.CU21_gestionar_promo_desc.routers.promociones import router as cu21_router
from app.modules.inventario_productos.cu22_gestionar_reservas_sucursal.routers.reservas import router as cu22_router
from app.modules.inventario_productos.cu23_gestionar_devoluciones.routers.devoluciones import router as cu23_router
from app.modules.inventario_productos.cu24_registrar_venta.routers.ventas import router as cu24_router
from app.modules.inventario_productos.cu25_reportes.routers.reportes import router as cu25_router


def create_app(settings=None, session_factory=None, recovery_delivery: RecoveryDelivery | None = None,
               pasarela: PasarelaPagos | None = None):
    @asynccontextmanager
    async def lifespan(application):
        try:
            config = settings if settings is not None else Settings()
        except ValidationError:
            # Do not print environment values from Pydantic's input diagnostics.
            raise RuntimeError("Configuración inválida: revise las variables de entorno requeridas") from None
        application.state.settings = config
        application.state.passwords = Passwords(config.password_storage)
        application.state.access_tokens = AccessTokens()
        # Pasarela de pago mock por defecto; inyectable en tests. Sin pasarelas reales.
        application.state.pasarela = pasarela if pasarela is not None else PasarelaMock()
        application.state.stripe = None
        if config.payments_provider == "stripe":
            try:
                application.state.stripe = StripeCheckout(
                    config.stripe_secret_key.get_secret_value(), config.stripe_webhook_secret.get_secret_value(),
                    config.stripe_currency, config.payments_frontend_url)
            except ValueError:
                raise RuntimeError("Configuración Stripe inválida: revise claves de prueba, moneda y webhook") from None
        engine = None
        if session_factory is None:
            engine, factory = create_database(config.database_url.get_secret_value())
        else:
            factory = session_factory
        application.state.session_factory = factory
        async def expire_reservations():
            from app.modules.inventario_productos.cu22_gestionar_reservas_sucursal.services.reservas import expire
            def run():
                with factory() as db:
                    expire(db)
            while True:
                try:
                    await asyncio.to_thread(run)
                except Exception:
                    logging.getLogger(__name__).warning('No se pudo ejecutar el vencimiento de reservas; se reintentará')
                await asyncio.sleep(60)
        expiry_task = asyncio.create_task(expire_reservations()) if config.environment != 'test' else None
        owned_delivery = configured_gmail_delivery(config) if recovery_delivery is None else None
        application.state.recovery_delivery = recovery_delivery if recovery_delivery is not None else owned_delivery
        try:
            yield
        finally:
            if expiry_task:
                expiry_task.cancel()
                try:
                    await expiry_task
                except asyncio.CancelledError:
                    pass
            if owned_delivery is not None:
                owned_delivery.close()
            application.state.access_tokens.clear()
            if engine is not None:
                engine.dispose()

    application = FastAPI(title="LiveClothesShop API", version="1.0.0", lifespan=lifespan)
    register_handlers(application)

    @application.middleware("http")
    async def protect_requests(request: Request, call_next):
        config = request.app.state.settings
        origin = request.headers.get("origin")
        is_auth = request.url.path.startswith("/api/auth/")
        is_cu05 = any(request.url.path == prefix or request.url.path.startswith(prefix + "/")
                      for prefix in ("/api/admin/usuarios", "/api/admin/empleados"))
        is_cu06 = any(request.url.path == prefix or request.url.path.startswith(prefix + "/")
                      for prefix in ("/api/admin/roles", "/api/admin/funciones"))
        is_cu07 = request.url.path == "/api/admin/clientes" or request.url.path.startswith("/api/admin/clientes/")
        is_cu08 = request.url.path == "/api/admin/bitacora" or request.url.path.startswith("/api/admin/bitacora/")
        is_cu09 = any(request.url.path == prefix or request.url.path.startswith(prefix + "/")
                      for prefix in ("/api/admin/ciudades", "/api/admin/sucursales"))
        is_catalogo = request.url.path == "/api/catalogo" or request.url.path.startswith("/api/catalogo/")
        is_cliente = request.url.path == "/api/cliente" or request.url.path.startswith("/api/cliente/")
        is_cu15_admin = request.url.path == "/api/admin/historial-compras" or request.url.path.startswith("/api/admin/historial-compras/")
        is_inventario_productos = any(request.url.path == prefix or request.url.path.startswith(prefix + '/')
                                     for prefix in ('/api/admin/catalogo', '/api/admin/proveedores', '/api/admin/inventario', '/api/admin/promociones', '/api/admin/reservas-sucursal', '/api/admin/devoluciones', '/api/admin/caja', '/api/admin/reportes'))
        protected = is_auth or is_cu05 or is_cu06 or is_cu07 or is_cu08 or is_cu09 or is_catalogo or is_cliente or is_cu15_admin or is_inventario_productos
        # Nativo móvil: Authorization Bearer sin cookie no usa defensa CSRF de
        # cookie (el token no se adjunta automáticamente). Con cookie presente
        # se mantiene Origin + cabecera; cookie+bearer se rechaza en dependencias.
        bearer_only = bool(request.headers.get("authorization")) and not request.cookies.get(config.cookie_name)
        if protected and request.method not in {"GET", "HEAD", "OPTIONS"} and not bearer_only:
            # Custom-header CSRF defense plus exact Origin allowlist, including
            # login CSRF. Cross-origin scripts require a successful preflight.
            if origin not in config.allowed_origins or request.headers.get("x-csrf-protection") != "1":
                return error_response(403, "origen_no_permitido", "Solicitud no permitida")
        if request.method == "OPTIONS" and protected:
            if origin not in config.allowed_origins:
                return error_response(403, "origen_no_permitido", "Solicitud no permitida")
            response = Response(status_code=204)
            response.headers["Access-Control-Allow-Methods"] = (
                "GET, POST, PUT, DELETE, OPTIONS" if is_inventario_productos else
                "GET, OPTIONS" if is_cu08 or is_cu15_admin else
                "GET, POST, PATCH, PUT, OPTIONS" if is_cu06 else
                "GET, POST, PATCH, DELETE, OPTIONS" if is_cliente else
                "GET, POST, PATCH, OPTIONS" if is_cu05 or is_cu09 else
                "GET, PATCH, OPTIONS" if is_cu07 else "GET, POST, OPTIONS")
            response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-CSRF-Protection, Authorization"
        else:
            try:
                response = await call_next(request)
            except Exception:
                # Never let an exception carrying SQL parameters reach ASGI logs.
                response = error_response(500, "error_interno", "No se pudo completar la operación")
        if protected:
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
            response.headers["X-Content-Type-Options"] = "nosniff"
            if origin in config.allowed_origins:
                response.headers["Access-Control-Allow-Origin"] = origin
                response.headers["Access-Control-Allow-Credentials"] = "true"
                response.headers["Vary"] = "Origin"
        return response

    @application.get("/")
    def root():
        return {"message": "LiveClothesShop API funcionando"}

    @application.get("/health")
    def health():
        return {"status": "ok"}

    application.include_router(router)
    application.include_router(cu05_router)
    application.include_router(cu06_router)
    application.include_router(cu07_router)
    application.include_router(cu08_router)
    application.include_router(cu09_router)
    application.include_router(cu10_router)
    application.include_router(cu11_router)
    application.include_router(cu11_sucursales_router)
    application.include_router(cu12_router)
    application.include_router(cu13_router)
    application.include_router(cu14_router)
    application.include_router(stripe_router)
    application.include_router(cu15_router)
    application.include_router(cu15_admin_router)
    application.include_router(cu18_router)
    application.include_router(cu19_router)
    application.include_router(cu20_router)
    application.include_router(cu21_router)
    application.include_router(cu22_router)
    application.include_router(cu23_router)
    application.include_router(cu24_router)
    application.include_router(cu25_router)
    return application


app = create_app()
