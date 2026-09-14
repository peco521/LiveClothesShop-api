from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from pydantic import ValidationError

from app.core.config import Settings
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
from app.integrations.payments.mock import PasarelaMock
from app.integrations.payments.protocolo import PasarelaPagos


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
        application.state.passwords = Passwords()
        # Explicit composition only. No implicit console/file/dev token delivery.
        application.state.recovery_delivery = recovery_delivery
        # Pasarela de pago mock por defecto; inyectable en tests. Sin pasarelas reales.
        application.state.pasarela = pasarela if pasarela is not None else PasarelaMock()
        engine = None
        if session_factory is None:
            engine, factory = create_database(config.database_url.get_secret_value())
        else:
            factory = session_factory
        application.state.session_factory = factory
        try:
            yield
        finally:
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
        protected = is_auth or is_cu05 or is_cu06 or is_cu07 or is_cu08 or is_cu09 or is_catalogo or is_cliente
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
                "GET, OPTIONS" if is_cu08 else
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
    return application


app = create_app()
