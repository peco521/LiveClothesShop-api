"""CU10 uses the shared catalog repository; no duplicate queries."""

from app.modules.cliente_experiencia_compra.shared.repositories import catalogo

__all__ = ["catalogo"]
