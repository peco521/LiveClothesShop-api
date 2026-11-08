-- FashionStore SaaS: auditoria atomica de ventas offline CU24.
-- Es un cambio de rutina para un esquema SaaS NUEVO; no migra filas de un
-- esquema anterior ni traduce venta.claveoperacion a UUID.
-- Aplicar únicamente después de 01..04. El bloque falla si el esquema no es
-- el contrato SaaS esperado.
DO $$
BEGIN
  IF to_regclass('public.venta') IS NULL
     OR NOT EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_schema='public' AND table_name='venta'
                      AND column_name='idoperacion' AND udt_name='uuid')
     OR to_regclass('public.operacion_sync') IS NULL
     OR to_regclass('public.bitacora') IS NULL THEN
    RAISE EXCEPTION 'ESQUEMA_SAAS_INCOMPATIBLE: se requiere venta.idOperacion UUID, operacion_sync y bitacora';
  END IF;
END;
$$;

CREATE OR REPLACE PROCEDURE sp_sync_confirmar(
  p_idEmp integer, p_uuid uuid, p_nroVenta integer DEFAULT NULL, p_nroReserva integer DEFAULT NULL
) LANGUAGE plpgsql AS $$
DECLARE
  v_usuario varchar;
BEGIN
  PERFORM fn_exigir_tenant(p_idEmp);
  IF (p_nroVenta IS NULL) = (p_nroReserva IS NULL) THEN
    RAISE EXCEPTION 'CONFIRMACION_INVALIDA: se requiere exactamente un documento';
  END IF;
  UPDATE operacion_sync
    SET estado='confirmada', codigoMotivo=NULL, detalleMotivo=NULL,
        nroVenta=COALESCE(p_nroVenta,nroVenta),
        nroReserva=COALESCE(p_nroReserva,nroReserva),
        fechaResolucion=now(), fechaUltimoIntento=now()
  WHERE idEmp=p_idEmp AND idOperacion=p_uuid;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'OPERACION_INEXISTENTE';
  END IF;
  IF p_nroVenta IS NOT NULL THEN
    SELECT idUsuarioEmp INTO v_usuario FROM venta
      WHERE idEmp=p_idEmp AND nroVenta=p_nroVenta AND idOperacion=p_uuid;
    IF NOT FOUND OR v_usuario IS NULL THEN
      RAISE EXCEPTION 'VENTA_SYNC_INCONSISTENTE';
    END IF;
    INSERT INTO bitacora(idEmp,usuario_id,accion,detalles)
    VALUES (p_idEmp,v_usuario,'venta_offline_confirmada',
            jsonb_build_object('idOperacion',p_uuid,'nroVenta',p_nroVenta));
  END IF;
END;
$$;
