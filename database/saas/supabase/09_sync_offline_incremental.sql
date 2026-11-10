-- Motor incremental de sincronización/offline. No reemplaza rutinas existentes:
-- se detiene si alguno de los contratos a crear ya existe.
\set ON_ERROR_STOP on
\if :{?target_db}
\else
  \echo 'Falta -v target_db=<base esperada>; no se aplicó la migración.'
  \quit 3
\endif
BEGIN;
SELECT set_config('fashionstore.expected_migration_db', :'target_db', true);
DO $$
DECLARE n text;
BEGIN
  IF current_database() <> current_setting('fashionstore.expected_migration_db') THEN RAISE EXCEPTION 'Destino inesperado'; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='fashionstore_app' AND NOT rolsuper AND NOT rolbypassrls) THEN
    RAISE EXCEPTION 'fashionstore_app incompatible';
  END IF;
  FOREACH n IN ARRAY ARRAY['fn_sync_tomar_operacion','fn_sync_estado_operacion','sp_sync_confirmar','sp_sync_rechazar','sp_sync_registrar_rechazo','sp_registrar_venta_offline','sp_resolver_conflicto_sync','sp_aprobar_cambio'] LOOP
    IF EXISTS (SELECT 1 FROM pg_proc WHERE pronamespace='public'::regnamespace AND proname=n) THEN
      RAISE EXCEPTION 'La rutina % ya existe; revisar su contrato antes de migrar', n;
    END IF;
  END LOOP;
END $$;

ALTER TABLE public.operacion_sync ADD COLUMN IF NOT EXISTS payload_hash char(64);
ALTER TABLE public.operacion_sync ADD COLUMN IF NOT EXISTS versionformato smallint NOT NULL DEFAULT 1;
ALTER TABLE public.operacion_sync ADD COLUMN IF NOT EXISTS fechaultimointento timestamptz;
ALTER TABLE public.operacion_sync DROP CONSTRAINT IF EXISTS operacion_sync_estado_check;
ALTER TABLE public.operacion_sync ADD CONSTRAINT operacion_sync_estado_check
  CHECK (estado IN ('recibida','en_proceso','confirmada','rechazada','conflicto'));
CREATE INDEX IF NOT EXISTS ix_operacion_sync_emp_fecha ON public.operacion_sync(idemp, fecharecepcion DESC);
CREATE INDEX IF NOT EXISTS ix_operacion_sync_conflictos ON public.operacion_sync(idemp, estado)
  WHERE estado IN ('conflicto','en_proceso');

CREATE FUNCTION public.fn_sync_tomar_operacion(
  p_idemp integer,p_uuid uuid,p_tipo varchar,p_nrosuc integer,p_idusuario varchar,
  p_fechalocal timestamptz,p_payload jsonb,p_hash char(64),p_iddispositivo varchar,p_version smallint
) RETURNS text LANGUAGE plpgsql AS $$
DECLARE v_estado varchar; v_hash char(64); v_fecha timestamptz;
BEGIN
  PERFORM public.fn_exigir_tenant(p_idemp);
  IF p_uuid IS NULL OR p_payload IS NULL OR p_hash IS NULL OR p_tipo NOT IN ('venta','reserva') THEN
    RAISE EXCEPTION 'DATOS_INVALIDOS: operacion de sincronizacion incompleta';
  END IF;
  INSERT INTO public.operacion_sync(idemp,idoperacion,tipo,nrosuc,idusuario,iddispositivo,fechalocal,payload,payload_hash,versionformato,estado)
  VALUES(p_idemp,p_uuid,p_tipo,p_nrosuc,p_idusuario,p_iddispositivo,p_fechalocal,p_payload,p_hash,p_version,'en_proceso')
  ON CONFLICT (idemp,idoperacion) DO NOTHING;
  IF FOUND THEN RETURN 'nueva'; END IF;
  SELECT estado,payload_hash,fecharecepcion INTO v_estado,v_hash,v_fecha
  FROM public.operacion_sync WHERE idemp=p_idemp AND idoperacion=p_uuid FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'NO_SE_PUDO_TOMAR: reintente'; END IF;
  IF v_hash IS NOT NULL AND v_hash IS DISTINCT FROM p_hash THEN RETURN 'conflicto_contenido'; END IF;
  IF v_estado='confirmada' THEN RETURN 'ya_confirmada'; END IF;
  IF v_estado IN ('rechazada','conflicto') THEN RETURN v_estado; END IF;
  IF v_estado='en_proceso' AND v_fecha < now()-interval '5 minutes' THEN
    UPDATE public.operacion_sync SET intentos=intentos+1,fechaultimointento=now(),iddispositivo=COALESCE(p_iddispositivo,iddispositivo)
    WHERE idemp=p_idemp AND idoperacion=p_uuid;
    RETURN 'nueva';
  END IF;
  RETURN 'en_proceso';
END $$;

CREATE PROCEDURE public.sp_sync_confirmar(p_idemp integer,p_uuid uuid,p_nroventa integer DEFAULT NULL,p_nroreserva integer DEFAULT NULL)
LANGUAGE plpgsql AS $$
DECLARE v_usuario varchar;
BEGIN
  PERFORM public.fn_exigir_tenant(p_idemp);
  IF (p_nroventa IS NULL) = (p_nroreserva IS NULL) THEN RAISE EXCEPTION 'CONFIRMACION_INVALIDA'; END IF;
  UPDATE public.operacion_sync SET estado='confirmada',codigomotivo=NULL,detallemotivo=NULL,
    nroventa=COALESCE(p_nroventa,nroventa),nroreserva=COALESCE(p_nroreserva,nroreserva),
    fecharesolucion=now(),fechaultimointento=now() WHERE idemp=p_idemp AND idoperacion=p_uuid;
  IF NOT FOUND THEN RAISE EXCEPTION 'OPERACION_INEXISTENTE'; END IF;
  IF p_nroventa IS NOT NULL THEN
    SELECT idusuarioemp INTO v_usuario FROM public.venta WHERE idemp=p_idemp AND nroventa=p_nroventa AND idoperacion=p_uuid;
    IF NOT FOUND OR v_usuario IS NULL THEN RAISE EXCEPTION 'VENTA_SYNC_INCONSISTENTE'; END IF;
    INSERT INTO public.bitacora(idemp,usuario_id,accion,detalles)
    VALUES(p_idemp,v_usuario,'venta_offline_confirmada',jsonb_build_object('idOperacion',p_uuid,'nroVenta',p_nroventa));
  END IF;
END $$;

CREATE PROCEDURE public.sp_sync_rechazar(p_idemp integer,p_uuid uuid,p_estado varchar,p_codigo varchar,p_detalle text DEFAULT NULL)
LANGUAGE plpgsql AS $$
BEGIN
  PERFORM public.fn_exigir_tenant(p_idemp);
  IF p_estado NOT IN ('rechazada','conflicto') THEN RAISE EXCEPTION 'ESTADO_INVALIDO'; END IF;
  UPDATE public.operacion_sync SET estado=CASE WHEN estado='confirmada' THEN 'confirmada' ELSE p_estado END,
    codigomotivo=CASE WHEN estado='confirmada' THEN codigomotivo ELSE p_codigo END,
    detallemotivo=CASE WHEN estado='confirmada' THEN detallemotivo ELSE p_detalle END,
    intentos=intentos+1,fechaultimointento=now(),fecharesolucion=now()
  WHERE idemp=p_idemp AND idoperacion=p_uuid;
  IF NOT FOUND THEN RAISE EXCEPTION 'OPERACION_INEXISTENTE'; END IF;
END $$;

CREATE FUNCTION public.fn_sync_estado_operacion(p_idemp integer,p_uuid uuid)
RETURNS TABLE(estado varchar,codigomotivo varchar,detallemotivo text,intentos integer,nroventa integer,nroreserva integer,fecharecepcion timestamptz,fecharesolucion timestamptz,payload_hash char(64))
LANGUAGE sql STABLE AS $$
  SELECT estado,codigomotivo,detallemotivo,intentos,nroventa,nroreserva,fecharecepcion,fecharesolucion,payload_hash
  FROM public.operacion_sync WHERE idemp=p_idemp AND idoperacion=p_uuid
$$;

CREATE PROCEDURE public.sp_sync_registrar_rechazo(
  p_idemp integer,p_uuid uuid,p_tipo varchar,p_nrosuc integer,p_idusuario varchar,p_fechalocal timestamptz,
  p_payload jsonb,p_hash char(64),p_iddispositivo varchar,p_version smallint,p_estado varchar,p_codigo varchar,p_detalle text
) LANGUAGE plpgsql AS $$
BEGIN
  PERFORM public.fn_exigir_tenant(p_idemp);
  IF p_tipo NOT IN ('venta','reserva') OR p_estado NOT IN ('rechazada','conflicto') THEN RAISE EXCEPTION 'ESTADO_INVALIDO'; END IF;
  INSERT INTO public.operacion_sync(idemp,idoperacion,tipo,nrosuc,idusuario,iddispositivo,fechalocal,payload,payload_hash,versionformato,estado,codigomotivo,detallemotivo,intentos,fechaultimointento,fecharesolucion)
  VALUES(p_idemp,p_uuid,p_tipo,p_nrosuc,p_idusuario,p_iddispositivo,p_fechalocal,p_payload,p_hash,p_version,p_estado,p_codigo,p_detalle,1,now(),now())
  ON CONFLICT(idemp,idoperacion) DO UPDATE SET
    estado=CASE WHEN operacion_sync.estado='confirmada' THEN 'confirmada' ELSE EXCLUDED.estado END,
    codigomotivo=CASE WHEN operacion_sync.estado='confirmada' THEN operacion_sync.codigomotivo ELSE EXCLUDED.codigomotivo END,
    detallemotivo=CASE WHEN operacion_sync.estado='confirmada' THEN operacion_sync.detallemotivo ELSE EXCLUDED.detallemotivo END,
    payload_hash=COALESCE(operacion_sync.payload_hash,EXCLUDED.payload_hash),intentos=operacion_sync.intentos+1,
    fechaultimointento=now(),fecharesolucion=now();
END $$;

CREATE PROCEDURE public.sp_registrar_venta_offline(
  p_idemp integer,p_nrosuc integer,p_idusuarioemp varchar,p_idusuariocl varchar,p_idvars varchar[],p_cantidades integer[],p_precios numeric[],
  p_idoperacion uuid,p_origen varchar,p_metodo varchar,p_descuento numeric,p_referencia varchar,INOUT p_nroventa integer DEFAULT NULL
) LANGUAGE plpgsql AS $$
DECLARE i integer; v_stock integer; v_precio numeric(10,2); v_base numeric(10,2):=0; v_desc numeric(10,2):=0; v_descunit numeric(10,2); v_nroinv integer; v_promo record; v_idpromo integer;
BEGIN
  PERFORM public.fn_exigir_tenant(p_idemp);
  IF p_origen NOT IN ('pwa','movil','servidor') THEN RAISE EXCEPTION 'ORIGEN_INVALIDO'; END IF;
  IF p_origen <> 'servidor' AND NOT public.fn_tiene_funcionalidad(p_idemp,'OFFLINE') THEN RAISE EXCEPTION 'FUNCIONALIDAD_NO_HABILITADA: OFFLINE'; END IF;
  IF p_metodo NOT IN ('efectivo','tarjeta','QR','transferencia') THEN RAISE EXCEPTION 'METODO_PAGO_INVALIDO'; END IF;
  IF p_idoperacion IS NULL AND p_origen <> 'servidor' THEN RAISE EXCEPTION 'UUID_REQUERIDO'; END IF;
  IF p_idoperacion IS NOT NULL THEN
    PERFORM pg_advisory_xact_lock(p_idemp,hashtext(p_idoperacion::text));
    SELECT nroventa INTO p_nroventa FROM public.venta WHERE idemp=p_idemp AND idoperacion=p_idoperacion;
    IF FOUND THEN RETURN; END IF;
  END IF;
  IF cardinality(p_idvars)=0 OR cardinality(p_idvars)<>cardinality(p_cantidades) OR cardinality(p_idvars)<>cardinality(p_precios)
     OR (SELECT count(DISTINCT x) FROM unnest(p_idvars) a(x))<>cardinality(p_idvars) THEN RAISE EXCEPTION 'DATOS_INVALIDOS: listas de variantes'; END IF;
  PERFORM 1 FROM public.empleado WHERE idemp=p_idemp AND idusuario=p_idusuarioemp AND nrosuc=p_nrosuc FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'SUCURSAL_NO_AUTORIZADA'; END IF;
  PERFORM 1 FROM public.sucursal WHERE idemp=p_idemp AND nro=p_nrosuc AND estado='activo';
  IF NOT FOUND THEN RAISE EXCEPTION 'SUCURSAL_INVALIDA'; END IF;
  PERFORM 1 FROM public.inventario WHERE idemp=p_idemp AND nrosuc=p_nrosuc AND idvar=ANY(p_idvars) ORDER BY idvar FOR UPDATE;
  FOR i IN 1..cardinality(p_idvars) LOOP
    SELECT inv.nroinv,inv.cantdisp,vp.precio,pr.idpromo INTO v_nroinv,v_stock,v_precio,v_idpromo
    FROM public.inventario inv JOIN public.varianteprod vp ON vp.idemp=inv.idemp AND vp.idvariante=inv.idvar
      JOIN public.producto pr ON pr.idemp=vp.idemp AND pr.idprod=vp.idprod
    WHERE inv.idemp=p_idemp AND inv.nrosuc=p_nrosuc AND inv.idvar=p_idvars[i];
    IF p_idvars[i] IS NULL OR p_cantidades[i] IS NULL OR p_cantidades[i]<=0 OR p_precios[i] IS NULL OR p_precios[i]<=0 OR NOT FOUND THEN RAISE EXCEPTION 'DATOS_INVALIDOS: linea %',i; END IF;
    IF v_stock<p_cantidades[i] THEN RAISE EXCEPTION 'STOCK_INSUFICIENTE: variante %',p_idvars[i]; END IF;
    IF round(p_precios[i],2)<>round(v_precio,2) THEN RAISE EXCEPTION 'PRECIO_CAMBIADO: variante %',p_idvars[i]; END IF;
    v_descunit:=0;
    IF v_idpromo IS NOT NULL THEN
      SELECT * INTO v_promo FROM public.promocion WHERE idemp=p_idemp AND idpromo=v_idpromo AND estado='activo' AND current_date BETWEEN fechaini AND fechafin;
      IF FOUND THEN v_descunit:=CASE WHEN v_promo.tipodescuento='porcentaje' THEN round(v_precio*v_promo.valordescuento/100,2) ELSE least(v_promo.valordescuento,v_precio) END; END IF;
    END IF;
    v_base:=v_base+round(p_cantidades[i]*v_precio,2); v_desc:=v_desc+round(v_descunit*p_cantidades[i],2);
  END LOOP;
  IF p_descuento IS NOT NULL AND round(p_descuento,2)<>round(v_desc,2) THEN RAISE EXCEPTION 'DESCUENTO_NO_AUTORIZADO: el descuento vigente calculado es %',v_desc; END IF;
  INSERT INTO public.venta(idemp,fechahora,total,desc_aplicado,estado,idusuariocl,idusuarioemp,nrosuc,idoperacion,origen,fechasync)
  VALUES(p_idemp,localtimestamp,v_base-v_desc,v_desc,'registrada',p_idusuariocl,p_idusuarioemp,p_nrosuc,p_idoperacion,p_origen,CASE WHEN p_origen='servidor' THEN NULL ELSE now() END) RETURNING nroventa INTO p_nroventa;
  FOR i IN 1..cardinality(p_idvars) LOOP
    SELECT nroinv INTO v_nroinv FROM public.inventario WHERE idemp=p_idemp AND nrosuc=p_nrosuc AND idvar=p_idvars[i];
    INSERT INTO public.detalleventa(idemp,nroventa,iddetalleventa,cantidad,preciounitario,idvar,descuentounitario) VALUES(p_idemp,p_nroventa,i,p_cantidades[i],p_precios[i],p_idvars[i],v_descunit);
    UPDATE public.inventario SET stock=stock-p_cantidades[i],cantdisp=cantdisp-p_cantidades[i] WHERE idemp=p_idemp AND nrosuc=p_nrosuc AND idvar=p_idvars[i];
    INSERT INTO public.movimiento_inv(idemp,tipomov,cantidad,fecha,motivo,nroinv) VALUES(p_idemp,'salida',p_cantidades[i],current_date,'Venta '||p_origen||' #'||p_nroventa,v_nroinv);
  END LOOP;
  INSERT INTO public.pago(idemp,metodo,monto,estado,referencia,nroventa) VALUES(p_idemp,p_metodo,v_base-v_desc,CASE WHEN p_metodo='efectivo' THEN 'aprobado' ELSE 'pendiente' END,p_referencia,p_nroventa);
END $$;

CREATE PROCEDURE public.sp_resolver_conflicto_sync(p_idemp integer,p_uuid uuid,p_idresponsable varchar,p_estado varchar,p_detalle text)
LANGUAGE plpgsql AS $$
BEGIN
  PERFORM public.fn_exigir_tenant(p_idemp);
  IF p_estado NOT IN ('rechazada','confirmada') THEN RAISE EXCEPTION 'ESTADO_INVALIDO'; END IF;
  UPDATE public.operacion_sync SET estado=p_estado,resueltapor=p_idresponsable,fecharesolucion=now(),detallemotivo=p_detalle
  WHERE idemp=p_idemp AND idoperacion=p_uuid AND estado='conflicto';
  IF NOT FOUND THEN RAISE EXCEPTION 'Conflicto inexistente o ya resuelto'; END IF;
END $$;

CREATE PROCEDURE public.sp_aprobar_cambio(p_idemp integer,p_nrocambio integer)
LANGUAGE plpgsql AS $$
BEGIN
  PERFORM public.fn_exigir_tenant(p_idemp);
  UPDATE public.cambio SET estado='aprobado' WHERE idemp=p_idemp AND nrocambio=p_nrocambio AND estado='pendiente';
  IF NOT FOUND THEN RAISE EXCEPTION 'Cambio inexistente o ya procesado'; END IF;
END $$;

REVOKE ALL ON FUNCTION public.fn_sync_tomar_operacion(integer,uuid,varchar,integer,varchar,timestamptz,jsonb,char(64),varchar,smallint),public.fn_sync_estado_operacion(integer,uuid) FROM PUBLIC;
REVOKE ALL ON PROCEDURE public.sp_sync_confirmar(integer,uuid,integer,integer),public.sp_sync_rechazar(integer,uuid,varchar,varchar,text),public.sp_sync_registrar_rechazo(integer,uuid,varchar,integer,varchar,timestamptz,jsonb,char(64),varchar,smallint,varchar,varchar,text),public.sp_registrar_venta_offline(integer,integer,varchar,varchar,varchar[],integer[],numeric[],uuid,varchar,varchar,numeric,varchar,integer),public.sp_resolver_conflicto_sync(integer,uuid,varchar,varchar,text),public.sp_aprobar_cambio(integer,integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.fn_sync_tomar_operacion(integer,uuid,varchar,integer,varchar,timestamptz,jsonb,char(64),varchar,smallint),public.fn_sync_estado_operacion(integer,uuid) TO fashionstore_app;
GRANT EXECUTE ON PROCEDURE public.sp_sync_confirmar(integer,uuid,integer,integer),public.sp_sync_rechazar(integer,uuid,varchar,varchar,text),public.sp_sync_registrar_rechazo(integer,uuid,varchar,integer,varchar,timestamptz,jsonb,char(64),varchar,smallint,varchar,varchar,text),public.sp_registrar_venta_offline(integer,integer,varchar,varchar,varchar[],integer[],numeric[],uuid,varchar,varchar,numeric,varchar,integer),public.sp_resolver_conflicto_sync(integer,uuid,varchar,varchar,text),public.sp_aprobar_cambio(integer,integer) TO fashionstore_app;
COMMIT;
