"""CU09: validación de direcciones con OpenStreetMap/Nominatim.

Sin claves de API: el backend es el único que consulta al proveedor, así que
ninguna credencial ni URL interna llega al frontend. Se distingue entre
"dirección no encontrada" (dato del usuario) y "proveedor no disponible"
(timeout, HTTP, JSON inesperado).
"""
import json
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode
from urllib.request import Request, build_opener


class AddressNotFound(Exception):
    """El proveedor respondió, pero no conoce la dirección indicada."""


class GeocodingUnavailable(Exception):
    """Timeout, error del proveedor o respuesta con formato inesperado."""


class NominatimGeocoder:
    """Adaptador mínimo de la API pública de búsqueda de Nominatim."""

    def __init__(self, base_url, user_agent, timeout=5):
        self._base_url = base_url.rstrip("/")
        self._user_agent = user_agent
        self._timeout = timeout

    def locate(self, direccion: str, ciudad: str):
        """Devuelve (latitud, longitud) con precisión numeric(9,6)."""
        query = urlencode({"q": f"{direccion}, {ciudad}", "format": "jsonv2", "limit": 1})
        request = Request(f"{self._base_url}/search?{query}",
                          headers={"User-Agent": self._user_agent, "Accept": "application/json"})
        try:
            with build_opener().open(request, timeout=self._timeout) as response:
                payload = json.loads(response.read(200_000))
        except Exception:
            # Nunca propagar detalles del proveedor (URL, cuerpo, cabeceras).
            raise GeocodingUnavailable() from None
        if not isinstance(payload, list):
            raise GeocodingUnavailable()
        if not payload:
            raise AddressNotFound()
        try:
            latitud = Decimal(str(payload[0]["lat"])).quantize(Decimal("0.000001"))
            longitud = Decimal(str(payload[0]["lon"])).quantize(Decimal("0.000001"))
        except (KeyError, IndexError, TypeError, InvalidOperation, ValueError):
            raise GeocodingUnavailable() from None
        if not latitud.is_finite() or not longitud.is_finite():
            raise GeocodingUnavailable()
        if not Decimal("-90") <= latitud <= Decimal("90") or not Decimal("-180") <= longitud <= Decimal("180"):
            raise GeocodingUnavailable()
        return latitud, longitud


def configured_geocoder(settings):
    """Adaptador según configuración; None desactiva la validación por completo."""
    if settings.geocoding_provider != "nominatim":
        return None
    return NominatimGeocoder(settings.geocoding_base_url, settings.geocoding_user_agent,
                             settings.geocoding_timeout)