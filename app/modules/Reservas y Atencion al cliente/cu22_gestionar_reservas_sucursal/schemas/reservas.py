from datetime import date, time
from typing import Literal
from app.modules.inventario_productos.shared.operaciones_schemas import Input


class ReservationAction(Input):
    accion: Literal['confirmar', 'atender', 'reprogramar']
    fechaReserva: date | None = None
    horaAtencion: time | None = None
