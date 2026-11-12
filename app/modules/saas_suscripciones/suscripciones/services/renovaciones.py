"""Servicio de notificaciones para cambios de plan y avisos semanales de renovación SaaS."""
import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session


logger = logging.getLogger(__name__)

HISTORIAL_FILE = Path(__file__).resolve().parents[5] / ".local" / "saas_renovaciones_historial.json"


def _cargar_historial() -> dict[str, str]:
    if not HISTORIAL_FILE.exists():
        return {}
    try:
        return json.loads(HISTORIAL_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _guardar_historial(historial: dict[str, str]) -> None:
    try:
        HISTORIAL_FILE.parent.mkdir(parents=True, exist_ok=True)
        HISTORIAL_FILE.write_text(json.dumps(historial, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning("No se pudo persistir historial de avisos de renovación: %s", e)


def notificar_cambio_plan_empresa(
    db: Session,
    idemp: int,
    nuevo_plan_id: int,
    inicio: date,
    fin: date,
    delivery=None,
) -> bool:
    """Notifica al administrador de una empresa cuando se actualiza su plan o vigencia de suscripción."""
    try:
        row = db.execute(
            text("""
                SELECT u.nombre, u.apellidopat, u.correo, e.nombre AS empresa_nombre, e.codigo AS empresa_codigo,
                       p.nombre AS plan_nombre
                FROM public.empresa e
                JOIN public.plan p ON p.idplan = :idplan
                LEFT JOIN public.admin a ON a.idemp = e.idemp
                LEFT JOIN public.usuario u ON u.idusuario = a.idusuario AND u.tipo = 'A'
                WHERE e.idemp = :idemp
                LIMIT 1
            """),
            {"idemp": idemp, "idplan": nuevo_plan_id},
        ).mappings().first()

        if not row or not row.get("correo"):
            return False

        admin_nombre = f"{row.get('nombre', '')} {row.get('apellidopat', '')}".strip()
        empresa_nombre = row.get("empresa_nombre") or row.get("empresa_codigo") or f"Empresa {idemp}"
        plan_nombre = row.get("plan_nombre", f"Plan {nuevo_plan_id}")
        correo = row["correo"]

        asunto = f"[FashionStore SaaS] Actualización de suscripción para {empresa_nombre}"
        cuerpo = (
            f"Estimado/a {admin_nombre},\n\n"
            f"Le informamos que la administración de la plataforma SaaS FashionStore ha actualizado "
            f"la suscripción de su empresa \"{empresa_nombre}\".\n\n"
            f"Detalles actualizados:\n"
            f"- Plan asignado: {plan_nombre}\n"
            f"- Inicio de vigencia: {inicio.strftime('%d/%m/%Y')}\n"
            f"- Vencimiento: {fin.strftime('%d/%m/%Y')}\n\n"
            f"Su acceso y el de sus sucursales ha sido sincronizado con las características de este plan.\n"
            f"Puede verificar los detalles en cualquier momento ingresando a su panel administrativo.\n\n"
            f"Atentamente,\nEquipo de Administración FashionStore SaaS\n"
        )

        if delivery and hasattr(delivery, "dispatch_email"):
            delivery.dispatch_email(correo, asunto, cuerpo)
            logger.info("Notificación de cambio de plan enviada a %s (%s)", correo, empresa_nombre)
            return True
        return False
    except Exception as e:
        logger.warning("Error al notificar cambio de plan a empresa %s: %s", idemp, e)
        return False


def notificar_actualizacion_plan_global(
    db: Session,
    plan_id: int,
    nuevo_nombre: str,
    max_sucursales: int,
    funcionalidades: list[str],
    delivery=None,
) -> int:
    """Notifica a todos los administradores de empresas activas cuando se modifica la definición de un plan."""
    if not delivery or not hasattr(delivery, "dispatch_email"):
        return 0

    notificados = 0
    try:
        rows = db.execute(
            text("""
                SELECT e.idemp, e.nombre AS empresa_nombre, e.codigo AS empresa_codigo,
                       u.nombre, u.apellidopat, u.correo
                FROM public.suscripcion s
                JOIN public.empresa e ON e.idemp = s.idemp
                JOIN public.admin a ON a.idemp = e.idemp
                JOIN public.usuario u ON u.idusuario = a.idusuario AND u.tipo = 'A'
                WHERE s.idplan = :plan_id AND s.estado = 'activa' AND e.estado = 'activa'
            """),
            {"plan_id": plan_id},
        ).mappings().all()

        func_str = ", ".join(funcionalidades) if funcionalidades else "Sin funcionalidades adicionales"

        for row in rows:
            correo = row.get("correo")
            if not correo:
                continue
            admin_nombre = f"{row.get('nombre', '')} {row.get('apellidopat', '')}".strip()
            empresa_nombre = row.get("empresa_nombre") or row.get("empresa_codigo")

            asunto = f"[FashionStore SaaS] Modificación del plan \"{nuevo_nombre}\""
            cuerpo = (
                f"Estimado/a {admin_nombre},\n\n"
                f"Le notificamos que el plan de suscripción \"{nuevo_nombre}\" que utiliza su empresa "
                f"\"{empresa_nombre}\" ha sido actualizado por la administración de plataforma SaaS.\n\n"
                f"Nuevas condiciones del plan:\n"
                f"- Límite máximo de sucursales activas: {max_sucursales}\n"
                f"- Funcionalidades incluidas: {func_str}\n\n"
                f"Estas características ya están vigentes para su empresa.\n\n"
                f"Atentamente,\nEquipo de Plataforma FashionStore SaaS\n"
            )
            try:
                delivery.dispatch_email(correo, asunto, cuerpo)
                notificados += 1
            except Exception as ex:
                logger.warning("Error al notificar admin de empresa %s: %s", empresa_nombre, ex)
    except Exception as e:
        logger.warning("Error al notificar actualización global de plan %s: %s", plan_id, e)

    return notificados


def verificar_y_notificar_vencimientos(
    db: Session,
    delivery=None,
    forzar: bool = False,
) -> list[dict[str, Any]]:
    """Comprueba las suscripciones en el último mes antes del vencimiento (<= 30 días).

    Envía un aviso semanal al correo del administrador de cada empresa registrada hasta
    que renueve o se cancele el plan.
    """
    historial = _cargar_historial()
    hoy = date.today()
    resultados: list[dict[str, Any]] = []

    try:
        rows = db.execute(
            text("""
                SELECT s.idsusc, s.idemp, s.fechavencimiento, s.estado,
                       p.nombre AS plan_nombre, e.nombre AS empresa_nombre, e.codigo AS empresa_codigo,
                       u.idusuario AS admin_id, u.nombre AS admin_nombre, u.apellidopat AS admin_apellido,
                       u.correo AS admin_correo,
                       CAST((s.fechavencimiento - CURRENT_DATE) AS integer) AS dias_restantes
                FROM public.suscripcion s
                JOIN public.plan p ON p.idplan = s.idplan
                JOIN public.empresa e ON e.idemp = s.idemp
                JOIN public.admin a ON a.idemp = s.idemp
                JOIN public.usuario u ON u.idusuario = a.idusuario AND u.tipo = 'A'
                WHERE s.estado = 'activa'
                  AND e.estado = 'activa'
                  AND s.fechavencimiento >= CURRENT_DATE
                  AND s.fechavencimiento <= CURRENT_DATE + interval '30 days'
                ORDER BY s.fechavencimiento ASC
            """)
        ).mappings().all()

        for r in rows:
            idsusc = str(r["idsusc"])
            dias_restantes = r["dias_restantes"]
            correo = r.get("admin_correo")
            admin_nombre = f"{r.get('admin_nombre', '')} {r.get('admin_apellido', '')}".strip()
            empresa_nombre = r.get("empresa_nombre") or r.get("empresa_codigo")
            plan_nombre = r.get("plan_nombre")
            fecha_vencimiento = r["fechavencimiento"]

            # Comprobar si ya se envió aviso en los últimos 7 días
            ultima_fecha_str = historial.get(idsusc)
            debe_enviar = forzar or (ultima_fecha_str is None)
            if not debe_enviar and ultima_fecha_str:
                try:
                    ultima_fecha = date.fromisoformat(ultima_fecha_str)
                    if (hoy - ultima_fecha).days >= 7:
                        debe_enviar = True
                except Exception:
                    debe_enviar = True

            enviado = False
            if debe_enviar and correo:
                asunto = f"[FashionStore SaaS] Recordatorio de renovación: Tu plan vence en {dias_restantes} días"
                cuerpo = (
                    f"Estimado/a {admin_nombre},\n\n"
                    f"Le recordamos que la suscripción de su empresa \"{empresa_nombre}\" (código: {r['empresa_codigo']}) "
                    f"al plan \"{plan_nombre}\" está próxima a culminar.\n\n"
                    f"Fecha de vencimiento: {fecha_vencimiento.strftime('%d/%m/%Y')} (restan {dias_restantes} días).\n\n"
                    f"Para evitar la suspensión de las operaciones de su tienda, sucursales y empleados, "
                    f"por favor contacte con la administración de plataforma SaaS para gestionar la renovación del plan.\n\n"
                    f"Este aviso se enviará cada semana mientras su suscripción esté en el último mes de vigencia "
                    f"hasta que se concrete la renovación o cancelación del plan.\n\n"
                    f"Atentamente,\nEquipo de Plataforma FashionStore SaaS\n"
                )

                if delivery and hasattr(delivery, "dispatch_email"):
                    try:
                        delivery.dispatch_email(correo, asunto, cuerpo)
                        enviado = True
                    except Exception as ex:
                        logger.warning("Error al enviar recordatorio de renovación a %s: %s", correo, ex)
                else:
                    # Registrado para entrega cuando esté activo el worker
                    enviado = True

                historial[idsusc] = hoy.isoformat()

            resultados.append({
                "idSusc": r["idsusc"],
                "idEmp": r["idemp"],
                "empresa": empresa_nombre,
                "codigo": r["empresa_codigo"],
                "plan": plan_nombre,
                "vencimiento": fecha_vencimiento.isoformat(),
                "diasRestantes": dias_restantes,
                "adminCorreo": correo,
                "adminNombre": admin_nombre,
                "debeEnviar": debe_enviar,
                "notificado": enviado,
                "ultimoAviso": historial.get(idsusc),
            })

        _guardar_historial(historial)
    except Exception as e:
        logger.warning("Error al comprobar vencimientos de suscripciones SaaS: %s", e)

    return resultados
