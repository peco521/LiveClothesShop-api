from ipaddress import ip_address
from typing import Literal

from app.modules.seguridad_accesos.repositories import bitacora


def record_role(db, action: Literal["rol_creado", "rol_actualizado", "permisos_rol_actualizados",
                                    "rol_activado", "rol_desactivado"],
                user_id: str, peer: str | None, role_id: str, *, agregadas: list[str] | None = None,
                retiradas: list[str] | None = None):
    # Dedicated allowlist: never accept request metadata or free-form descriptions.
    if action not in {"rol_creado", "rol_actualizado", "permisos_rol_actualizados",
                      "rol_activado", "rol_desactivado"}:
        raise ValueError("Acción de rol inválida")
    try:
        ip = str(ip_address(peer)) if peer else None
    except ValueError:
        ip = None
    details = {"resultado": "exito", "rol": role_id}
    if action == "permisos_rol_actualizados":
        details.update(agregadas=sorted(agregadas or []), retiradas=sorted(retiradas or []))
    bitacora.add(db, action=action, user_id=user_id, ip=ip, details=details)


def record(db, action: Literal["cliente_registrado", "login_correcto", "login_rechazado", "logout_correcto", "recuperacion_solicitada", "contrasena_restablecida",
                              "usuario_creado", "empleado_creado", "usuario_actualizado", "empleado_actualizado", "usuario_activado", "usuario_desactivado",
                               "cliente_actualizado", "cliente_activado", "cliente_desactivado",
                               "ciudad_creada", "ciudad_actualizada", "sucursal_creada", "sucursal_actualizada", "sucursal_estado_actualizado",
                               "reserva_creada", "reserva_cancelada", "reserva_vencida",
                               "carrito_item_agregado", "carrito_item_actualizado", "carrito_item_eliminado",
                               "venta_registrada", "pago_iniciado", "pago_aprobado", "pago_rechazado",
                               "venta_anulada", "catalogo_guardado", "catalogo_eliminado", "proveedor_guardado",
                               "proveedor_eliminado", "inventario_movimiento_registrado"],
           user_id: str | None, peer: str | None, success: bool):
    try:
        ip = str(ip_address(peer)) if peer else None
    except ValueError:
        ip = None
    # Fixed allowlist: no arbitrary request payload, metadata or credentials.
    bitacora.add(db, action=action, user_id=user_id, ip=ip,
                 details={"resultado": "exito" if success else "rechazado"})
