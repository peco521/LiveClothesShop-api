"""Pasarela mock académica y determinista (CU14, entorno de prueba).

Sin integraciones reales, sin aleatoriedad, sin datos financieros:
- `escenario=None` (defecto): aprueba siempre.
- `escenario="rechazado"`: rechaza siempre, sin referencia.
- `escenario="timeout"`: lanza TimeoutPasarela (resultado desconocido).

La referencia es estable por pago: MOCK-<idPago>-<nroVenta>. No sensible.
"""

from decimal import Decimal

from app.integrations.payments.protocolo import (
    EscenarioMock,
    PasarelaPagos,
    ResultadoPasarela,
    TimeoutPasarela,
)


class PasarelaMock(PasarelaPagos):
    def cobrar(self, *, monto: Decimal, metodo: str, nro_venta: int, id_pago: int,
               escenario: EscenarioMock | None = None) -> ResultadoPasarela:
        if escenario == "timeout":
            raise TimeoutPasarela("Sin respuesta de la pasarela")
        if escenario == "rechazado":
            return ResultadoPasarela("rechazado", None)
        referencia = f"MOCK-{id_pago:06d}-{nro_venta}"
        return ResultadoPasarela("aprobado", referencia)
