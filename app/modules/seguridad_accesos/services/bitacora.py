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
                               "reserva_confirmar", "reserva_atender", "reserva_reprogramar",
                               "politica_devolucion_guardada", "devolucion_registrada",
                               "devolucion_inspeccionada", "devolucion_aprobada",
                               "devolucion_rechazada", "reembolso_efectivo_entregado",
                               "reembolso_consultado", "cambio_registrado",
                               "cambio_aprobado", "cambio_rechazado",
                               "carrito_item_agregado", "carrito_item_actualizado", "carrito_item_eliminado",
                               "venta_registrada", "pago_iniciado", "pago_aprobado", "pago_rechazado",
                                "venta_anulada", "catalogo_guardado", "catalogo_eliminado", "proveedor_guardado",
                               "proveedor_eliminado", "inventario_movimiento_registrado",
                               "venta_caja_preparada", "venta_caja_pagada", "venta_caja_pago_electronico",
                               "venta_caja_cancelada"],
           user_id: str | None, peer: str | None, success: bool, *, resource_id: str | None = None):
    try:
        ip = str(ip_address(peer)) if peer else None
    except ValueError:
        ip = None
    # Fixed allowlist: no arbitrary request payload, metadata or credentials.
    details = {"resultado": "exito" if success else "rechazado"}
    if resource_id is not None:
        if action in {"cliente_registrado", "cliente_actualizado",
                      "cliente_activado", "cliente_desactivado"}:
            details["idUsuario"] = resource_id
        elif action in {"reserva_vencida", "reserva_confirmar",
                        "reserva_atender", "reserva_reprogramar"}:
            details["nroReserva"] = resource_id
        elif action.startswith("devolucion_"):
            details["nroDev"] = resource_id
        elif action.startswith("cambio_"):
            details["nroCambio"] = resource_id
        elif action.startswith("reembolso_"):
            details["idReembolso"] = resource_id
        elif action.startswith("pago_"):
            details["idPago"] = resource_id
        else:
            raise ValueError("La acción no admite este recurso")
    bitacora.add(db, action=action, user_id=user_id, ip=ip,
                 details=details)
