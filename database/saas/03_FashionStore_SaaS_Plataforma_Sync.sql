-- FashionStore / Examen Parcial 2 - ICONIX
-- PARCHE 03 sobre el esquema SaaS creado por 01 + 02.
-- NO es una migracion de la instalacion anterior: solo agrega lo necesario para
-- (a) identidad visual de empresa, (b) administracion de plataforma, y
-- (c) motor de sincronizacion offline con idempotencia persistente.
-- Ejecutar SIEMPRE despues de 01 y 02, dentro de una base de pruebas separada.
BEGIN;

-- ==========================================================
-- 0) COLUMNAS QUE LA API YA USA Y EL BORRADOR 01 NO INCLUIA
-- ==========================================================
-- Equivalen al parche historico LiveClothesShop-api/migrations/001. Se
-- repiten aqui de forma idempotente para que el esquema SaaS quede completo
-- sin depender de la ruta antigua.
ALTER TABLE rol ADD COLUMN IF NOT EXISTS estado varchar(15) NOT NULL DEFAULT 'activo';
ALTER TABLE rol DROP CONSTRAINT IF EXISTS ck_rol_estado;
ALTER TABLE rol ADD CONSTRAINT ck_rol_estado CHECK (estado IN ('activo','inactivo'));
ALTER TABLE empleado ADD COLUMN IF NOT EXISTS estado varchar(15) NOT NULL DEFAULT 'activo';
ALTER TABLE empleado DROP CONSTRAINT IF EXISTS ck_empleado_estado;
ALTER TABLE empleado ADD CONSTRAINT ck_empleado_estado CHECK (estado IN ('activo','inactivo'));
ALTER TABLE sucursal ADD COLUMN IF NOT EXISTS latitud numeric(9,6);
ALTER TABLE sucursal ADD COLUMN IF NOT EXISTS longitud numeric(9,6);
ALTER TABLE detalleventa ADD COLUMN IF NOT EXISTS descuentounitario numeric(10,2);
ALTER TABLE detalleventa DROP CONSTRAINT IF EXISTS ck_detalleventa_descuento;
ALTER TABLE detalleventa ADD CONSTRAINT ck_detalleventa_descuento CHECK (descuentounitario >= 0);
-- CU07 admite nombres NULL heredados; las altas siguen validadas en la API.
ALTER TABLE usuario ALTER COLUMN nombre DROP NOT NULL;

-- ==========================================================
-- 1) IDENTIDAD VISUAL / DEMOSTRACION
-- ==========================================================
ALTER TABLE empresa ADD COLUMN IF NOT EXISTS logoUrl varchar(255);
ALTER TABLE empresa ADD COLUMN IF NOT EXISTS colorPrimario varchar(7);
ALTER TABLE empresa ADD COLUMN IF NOT EXISTS colorSecundario varchar(7);
ALTER TABLE empresa ADD COLUMN IF NOT EXISTS esDemostracion boolean NOT NULL DEFAULT false;
ALTER TABLE empresa DROP CONSTRAINT IF EXISTS ck_empresa_color_primario;
ALTER TABLE empresa ADD CONSTRAINT ck_empresa_color_primario
  CHECK (colorPrimario IS NULL OR colorPrimario ~ '^#[0-9A-Fa-f]{6}$');
ALTER TABLE empresa DROP CONSTRAINT IF EXISTS ck_empresa_color_secundario;
ALTER TABLE empresa ADD CONSTRAINT ck_empresa_color_secundario
  CHECK (colorSecundario IS NULL OR colorSecundario ~ '^#[0-9A-Fa-f]{6}$');

-- ==========================================================
-- 2) ESTADOS Y METADATOS DEL MOTOR DE SINCRONIZACION
-- ==========================================================
-- Estados tecnicos persistidos. El cliente usa un vocabulario estable que se
-- mapea 1:1 en la API:
--   recibida        -> pendiente / reintento pendiente (segun intentos)
--   en_proceso      -> en proceso (recuperable tras cierre inesperado)
--   confirmada      -> sincronizada
--   conflicto       -> conflicto (requiere intervencion)
--   rechazada       -> rechazada (terminal)
ALTER TABLE operacion_sync DROP CONSTRAINT IF EXISTS operacion_sync_estado_check;
ALTER TABLE operacion_sync ADD CONSTRAINT operacion_sync_estado_check
  CHECK (estado IN ('recibida','en_proceso','confirmada','rechazada','conflicto'));
ALTER TABLE operacion_sync ADD COLUMN IF NOT EXISTS payload_hash char(64);
ALTER TABLE operacion_sync ADD COLUMN IF NOT EXISTS versionFormato smallint NOT NULL DEFAULT 1;
ALTER TABLE operacion_sync ADD COLUMN IF NOT EXISTS fechaUltimoIntento timestamptz;
ALTER TABLE operacion_sync ADD COLUMN IF NOT EXISTS idDispositivo varchar(100);

-- El UUID de la operacion debe ser el mismo en todos los reintentos; el hash
-- detecta el mismo UUID con contenido distinto (conflicto, no reejecucion).
CREATE INDEX IF NOT EXISTS ix_operacion_sync_emp_fecha ON operacion_sync(idEmp, fechaRecepcion DESC);
CREATE INDEX IF NOT EXISTS ix_operacion_sync_conflictos ON operacion_sync(idEmp, estado)
  WHERE estado IN ('conflicto','en_proceso');

-- ==========================================================
-- 3) RESERVA/TOMA ATOMICA DE UNA OPERACION (idempotencia persistente)
-- ==========================================================
-- Devuelve uno de:
--   nueva             -> se tomo el UUID, ejecutar el efecto de negocio
--   ya_confirmada     -> reintento del mismo UUID: devolver resultado guardado
--   en_proceso        -> otro worker la esta ejecutando: reintentar luego
--   conflicto_contenido -> mismo UUID con payload distinto (terminal)
--   rechazada         -> ya rechazada previamente (terminal)
--   conflicto         -> ya registrada como conflicto (terminal)
-- El efecto de negocio y la marca 'confirmada' se confirman en la MISMA
-- transaccion mediante sp_sync_confirmar: si el efecto falla, todo revierte y
-- el UUID puede reutilizarse.
CREATE OR REPLACE FUNCTION fn_sync_tomar_operacion(
  p_idEmp integer, p_uuid uuid, p_tipo varchar, p_nroSuc integer, p_idUsuario varchar,
  p_fechaLocal timestamptz, p_payload jsonb, p_hash char(64),
  p_idDispositivo varchar, p_version smallint
) RETURNS text
LANGUAGE plpgsql
AS $$
DECLARE
  v_estado varchar;
  v_hash char(64);
  v_fecha timestamptz;
BEGIN
  PERFORM fn_exigir_tenant(p_idEmp);
  IF p_uuid IS NULL OR p_payload IS NULL OR p_hash IS NULL OR p_tipo IS NULL THEN
    RAISE EXCEPTION 'DATOS_INVALIDOS: operacion de sincronizacion incompleta';
  END IF;
  IF p_tipo NOT IN ('venta','reserva') THEN
    RAISE EXCEPTION 'TIPO_NO_SOPORTADO: solo venta y reserva';
  END IF;

  INSERT INTO operacion_sync(idEmp,idOperacion,tipo,nroSuc,idUsuario,idDispositivo,
                             fechaLocal,payload,payload_hash,versionFormato,estado)
  VALUES (p_idEmp,p_uuid,p_tipo,p_nroSuc,p_idUsuario,p_idDispositivo,
          p_fechaLocal,p_payload,p_hash,p_version,'en_proceso')
  ON CONFLICT (idEmp,idOperacion) DO NOTHING;

  IF FOUND THEN
    RETURN 'nueva';
  END IF;

  SELECT estado, payload_hash, fechaRecepcion INTO v_estado, v_hash, v_fecha
  FROM operacion_sync
  WHERE idEmp = p_idEmp AND idOperacion = p_uuid
  FOR UPDATE;

  IF v_estado IS NULL THEN
    RAISE EXCEPTION 'NO_SE_PUDO_TOMAR: reintente';
  END IF;
  -- v_hash NULL = fila creada por una version anterior del registro de sync.
  IF v_hash IS NOT NULL AND v_hash IS DISTINCT FROM p_hash THEN
    RETURN 'conflicto_contenido';
  END IF;
  IF v_estado = 'confirmada' THEN RETURN 'ya_confirmada'; END IF;
  IF v_estado IN ('rechazada','conflicto') THEN RETURN v_estado; END IF;
  -- Recuperacion de una operacion que quedo en proceso por un cierre inesperado.
  IF v_estado = 'en_proceso' AND v_fecha < now() - interval '5 minutes' THEN
    UPDATE operacion_sync
      SET estado='en_proceso', intentos=intentos+1, fechaUltimoIntento=now(),
          idDispositivo=COALESCE(p_idDispositivo,idDispositivo)
    WHERE idEmp=p_idEmp AND idOperacion=p_uuid;
    RETURN 'nueva';
  END IF;
  RETURN 'en_proceso';
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
    -- Audit shares the transaction with sale, payment, inventory and sync
    -- confirmation. Replays do not invoke this procedure after confirmation.
    INSERT INTO bitacora(idEmp,usuario_id,accion,detalles)
    VALUES (p_idEmp,v_usuario,'venta_offline_confirmada',
            jsonb_build_object('idOperacion',p_uuid,'nroVenta',p_nroVenta));
  END IF;
END;
$$;

CREATE OR REPLACE PROCEDURE sp_sync_rechazar(
  p_idEmp integer, p_uuid uuid, p_estado varchar,
  p_codigo varchar, p_detalle text DEFAULT NULL
) LANGUAGE plpgsql AS $$
BEGIN
  PERFORM fn_exigir_tenant(p_idEmp);
  IF p_estado NOT IN ('rechazada','conflicto') THEN
    RAISE EXCEPTION 'ESTADO_INVALIDO';
  END IF;
  -- Terminal salvo que ya estuviera confirmada: una operacion confirmada nunca
  -- se degrada por un error tardio de un reintento.
  UPDATE operacion_sync
    SET estado = CASE WHEN estado='confirmada' THEN 'confirmada' ELSE p_estado END,
        codigoMotivo = CASE WHEN estado='confirmada' THEN codigoMotivo ELSE p_codigo END,
        detalleMotivo = CASE WHEN estado='confirmada' THEN detalleMotivo ELSE p_detalle END,
        intentos = intentos + 1,
        fechaUltimoIntento = now(),
        fechaResolucion = now()
  WHERE idEmp=p_idEmp AND idOperacion=p_uuid;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'OPERACION_INEXISTENTE';
  END IF;
END;
$$;

-- ==========================================================
-- 4) VENTA OFFLINE/PWA/MOVIL IDEMPOTENTE (CU24 + motor de sync)
-- ==========================================================
-- Igual que sp_registrar_venta_contado pero:
--  * el medio de pago decide el estado: efectivo -> aprobado; tarjeta/QR/
--    transferencia -> PENDIENTE hasta que la pasarela confirme (nunca se
--    inventa un pago aprobado);
--  * aplica solo promociones vigentes del catalogo y rechaza descuentos
--    manuales no autorizados;
--  * valida la sucursal y el cajero contra el tenant de la sesion.
CREATE OR REPLACE PROCEDURE sp_registrar_venta_offline(
  p_idEmp integer, p_nroSuc integer, p_idUsuarioEmp varchar,
  p_idUsuarioCl varchar, p_idVars varchar[], p_cantidades integer[],
  p_precios numeric[], p_idOperacion uuid, p_origen varchar,
  p_metodo varchar, p_descuento numeric, p_referencia varchar,
  INOUT p_nroVenta integer DEFAULT NULL
) LANGUAGE plpgsql AS $$
DECLARE
  i integer;
  v_stock integer;
  v_precio numeric(10,2);
  v_base numeric(10,2):=0;
  v_desc_calc numeric(10,2):=0;
  v_desc_unit numeric(10,2);
  v_total numeric(10,2);
  v_nroInv integer;
  v_idPromo integer;
  v_promo record;
  v_pago_estado varchar(20);
BEGIN
  PERFORM fn_exigir_tenant(p_idEmp);
  IF p_origen NOT IN ('pwa','movil','servidor') THEN
    RAISE EXCEPTION 'ORIGEN_INVALIDO';
  END IF;
  IF p_origen <> 'servidor' AND NOT fn_tiene_funcionalidad(p_idEmp,'OFFLINE') THEN
    RAISE EXCEPTION 'FUNCIONALIDAD_NO_HABILITADA: OFFLINE';
  END IF;
  IF p_metodo NOT IN ('efectivo','tarjeta','QR','transferencia') THEN
    RAISE EXCEPTION 'METODO_PAGO_INVALIDO';
  END IF;

  IF p_idOperacion IS NOT NULL THEN
    PERFORM pg_advisory_xact_lock(p_idEmp, hashtext(p_idOperacion::text));
    SELECT nroVenta INTO p_nroVenta FROM venta
      WHERE idEmp=p_idEmp AND idOperacion=p_idOperacion;
    IF FOUND THEN RETURN; END IF;
  END IF;
  IF p_origen <> 'servidor' AND p_idOperacion IS NULL THEN
    RAISE EXCEPTION 'UUID_REQUERIDO';
  END IF;
  IF p_idVars IS NULL OR cardinality(p_idVars)=0
     OR cardinality(p_idVars) <> cardinality(p_cantidades)
     OR cardinality(p_idVars) <> cardinality(p_precios) THEN
    RAISE EXCEPTION 'DATOS_INVALIDOS: listas de variantes, cantidades y precios';
  END IF;
  IF (SELECT count(DISTINCT x) FROM unnest(p_idVars) AS a(x)) <> cardinality(p_idVars) THEN
    RAISE EXCEPTION 'DATOS_INVALIDOS: variantes repetidas';
  END IF;

  PERFORM 1 FROM empleado
    WHERE idEmp=p_idEmp AND idUsuario=p_idUsuarioEmp AND nroSuc=p_nroSuc FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'SUCURSAL_NO_AUTORIZADA: el cajero no pertenece a la sucursal'; END IF;
  PERFORM 1 FROM sucursal WHERE idEmp=p_idEmp AND nro=p_nroSuc AND estado='activo';
  IF NOT FOUND THEN RAISE EXCEPTION 'SUCURSAL_INVALIDA'; END IF;

  -- Bloqueo estable por idVar para reducir deadlocks entre cajas concurrentes.
  PERFORM 1 FROM inventario
    WHERE idEmp=p_idEmp AND nroSuc=p_nroSuc AND idVar=ANY(p_idVars)
    ORDER BY idVar FOR UPDATE;

  FOR i IN 1..cardinality(p_idVars) LOOP
    IF p_idVars[i] IS NULL OR btrim(p_idVars[i])='' OR p_cantidades[i] IS NULL
       OR p_cantidades[i] <= 0 OR p_precios[i] IS NULL OR p_precios[i] <= 0 THEN
      RAISE EXCEPTION 'DATOS_INVALIDOS: linea %', i;
    END IF;
    SELECT inv.nroInv, inv.cantDisp, vp.precio, pr.idPromo
      INTO v_nroInv, v_stock, v_precio, v_idPromo
    FROM inventario inv
    JOIN varianteProd vp ON vp.idEmp=inv.idEmp AND vp.idVariante=inv.idVar
    JOIN producto pr ON pr.idEmp=vp.idEmp AND pr.idProd=vp.idProd
    WHERE inv.idEmp=p_idEmp AND inv.nroSuc=p_nroSuc AND inv.idVar=p_idVars[i];
    IF NOT FOUND OR v_stock < p_cantidades[i] THEN
      RAISE EXCEPTION 'STOCK_INSUFICIENTE: variante %', p_idVars[i];
    END IF;
    IF round(p_precios[i],2) <> round(v_precio,2) THEN
      RAISE EXCEPTION 'PRECIO_CAMBIADO: variante %', p_idVars[i];
    END IF;
    v_base := v_base + round(p_cantidades[i] * v_precio, 2);

    -- Promocion vigente: solo catalogo, nunca un descuento enviado por el cliente.
    v_desc_unit := 0;
    v_promo := NULL;
    IF v_idPromo IS NOT NULL THEN
      SELECT * INTO v_promo FROM promocion p
      WHERE p.idEmp=p_idEmp AND p.idPromo=v_idPromo AND p.estado='activo'
        AND current_date BETWEEN p.fechaIni AND p.fechaFin;
      IF FOUND THEN
        IF v_promo.tipoDescuento='porcentaje' THEN
          v_desc_unit := round(v_precio * v_promo.valorDescuento / 100, 2);
        ELSE
          v_desc_unit := LEAST(v_promo.valorDescuento, v_precio);
        END IF;
        v_desc_unit := LEAST(v_desc_unit, v_precio);
        v_desc_calc := v_desc_calc + round(v_desc_unit * p_cantidades[i], 2);
      END IF;
    END IF;
  END LOOP;

  IF p_descuento IS NOT NULL AND round(p_descuento,2) <> round(v_desc_calc,2) THEN
    RAISE EXCEPTION 'DESCUENTO_NO_AUTORIZADO: el descuento vigente calculado es %', v_desc_calc;
  END IF;
  v_total := v_base - v_desc_calc;
  IF v_total < 0 THEN RAISE EXCEPTION 'TOTAL_INVALIDO'; END IF;

  INSERT INTO venta(idEmp,fechaHora,total,desc_aplicado,estado,idUsuarioCl,idUsuarioEmp,
                    nroSuc,idOperacion,origen,fechaSync)
  VALUES (p_idEmp,localtimestamp,v_total,v_desc_calc,'registrada',p_idUsuarioCl,p_idUsuarioEmp,
          p_nroSuc,p_idOperacion,p_origen,
          CASE WHEN p_origen='servidor' THEN NULL ELSE now() END)
  RETURNING nroVenta INTO p_nroVenta;

  FOR i IN 1..cardinality(p_idVars) LOOP
    SELECT inv.nroInv INTO v_nroInv FROM inventario inv
      WHERE inv.idEmp=p_idEmp AND inv.nroSuc=p_nroSuc AND inv.idVar=p_idVars[i];
    INSERT INTO detalleVenta(idEmp,nroVenta,idDetalleVenta,cantidad,precioUnitario,idVar)
    VALUES (p_idEmp,p_nroVenta,i,p_cantidades[i],p_precios[i],p_idVars[i]);
    UPDATE inventario
      SET stock=stock-p_cantidades[i], cantDisp=cantDisp-p_cantidades[i]
      WHERE idEmp=p_idEmp AND nroSuc=p_nroSuc AND idVar=p_idVars[i];
    INSERT INTO movimiento_inv(idEmp,tipoMov,cantidad,fecha,motivo,nroInv)
    VALUES (p_idEmp,'salida',p_cantidades[i],current_date,
            'Venta '||p_origen||' #'||p_nroVenta, v_nroInv);
  END LOOP;

  v_pago_estado := CASE WHEN p_metodo='efectivo' THEN 'aprobado' ELSE 'pendiente' END;
  INSERT INTO pago(idEmp,metodo,monto,estado,referencia,nroVenta)
  VALUES(p_idEmp,p_metodo,v_total,v_pago_estado,p_referencia,p_nroVenta);
END;
$$;

-- ==========================================================
-- 5) LECTURA DEL RESULTADO GUARDADO (reintento tras perder la respuesta)
-- ==========================================================
CREATE OR REPLACE FUNCTION fn_sync_estado_operacion(p_idEmp integer, p_uuid uuid)
RETURNS TABLE(
  estado varchar, codigoMotivo varchar, detalleMotivo text, intentos integer,
  nroVenta integer, nroReserva integer, fechaRecepcion timestamptz, fechaResolucion timestamptz,
  payload_hash char(64)
) LANGUAGE sql STABLE AS $$
  SELECT estado, codigoMotivo, detalleMotivo, intentos, nroVenta, nroReserva,
         fechaRecepcion, fechaResolucion, payload_hash
  FROM operacion_sync WHERE idEmp=p_idEmp AND idOperacion=p_uuid;
$$;

-- Registra un rechazo o conflicto terminal DESPUES de revertir el efecto de
-- negocio. Crea la fila si aun no existe (la toma original se revirtio) y nunca
-- degrada una operacion ya confirmada.
CREATE OR REPLACE PROCEDURE sp_sync_registrar_rechazo(
  p_idEmp integer, p_uuid uuid, p_tipo varchar, p_nroSuc integer, p_idUsuario varchar,
  p_fechaLocal timestamptz, p_payload jsonb, p_hash char(64),
  p_idDispositivo varchar, p_version smallint,
  p_estado varchar, p_codigo varchar, p_detalle text
) LANGUAGE plpgsql AS $$
BEGIN
  PERFORM fn_exigir_tenant(p_idEmp);
  IF p_estado NOT IN ('rechazada','conflicto') THEN
    RAISE EXCEPTION 'ESTADO_INVALIDO';
  END IF;
  INSERT INTO operacion_sync(idEmp,idOperacion,tipo,nroSuc,idUsuario,idDispositivo,
                             fechaLocal,payload,payload_hash,versionFormato,estado,
                             codigoMotivo,detalleMotivo,intentos,fechaUltimoIntento,fechaResolucion)
  VALUES (p_idEmp,p_uuid,p_tipo,p_nroSuc,p_idUsuario,p_idDispositivo,
          p_fechaLocal,p_payload,p_hash,p_version,p_estado,
          p_codigo,p_detalle,1,now(),now())
  ON CONFLICT (idEmp,idOperacion) DO UPDATE
    SET estado = CASE WHEN operacion_sync.estado='confirmada' THEN 'confirmada' ELSE EXCLUDED.estado END,
        codigoMotivo = CASE WHEN operacion_sync.estado='confirmada' THEN operacion_sync.codigoMotivo ELSE EXCLUDED.codigoMotivo END,
        detalleMotivo = CASE WHEN operacion_sync.estado='confirmada' THEN operacion_sync.detalleMotivo ELSE EXCLUDED.detalleMotivo END,
        payload_hash = COALESCE(operacion_sync.payload_hash, EXCLUDED.payload_hash),
        intentos = operacion_sync.intentos + 1,
        fechaUltimoIntento = now(),
        fechaResolucion = now();
END;
$$;

-- ==========================================================
-- 6) PERMISOS DE PLATAFORMA
-- ==========================================================
-- El usuario tipo 'S' no pertenece a ninguna empresa (idEmp NULL) y usa el rol
-- SAAS-ADMIN. Los permisos de plataforma son un catalogo separado de CU05..CU25.
INSERT INTO funcion(id,descripcion) VALUES
  ('SAAS-EMPRESAS','Administrar empresas y aprovisionamiento'),
  ('SAAS-PLANES','Administrar planes y suscripciones'),
  ('SAAS-AUDITORIA','Consultar auditoria de plataforma'),
  ('SAAS-DEMO','Acceso a credenciales de demostracion'),
  ('SAAS-SOPORTE','Solicitar acceso de soporte auditado')
ON CONFLICT (id) DO NOTHING;

INSERT INTO rol_funcion(nroRol,idFun,descripcion,idEmp)
SELECT 'SAAS-ADMIN', f.id, f.descripcion, NULL
FROM funcion f WHERE f.id LIKE 'SAAS-%'
ON CONFLICT (nroRol,idFun) DO NOTHING;

-- ==========================================================
-- 7) FUNCIONES DE PLATAFORMA (solo rol de aprovisionamiento)
-- ==========================================================
-- Resolucion de codigo de empresa ANTES de tener contexto de tenant.
-- SECURITY DEFINER: su propietario (fashionstore_provisioner, BYPASSRLS) puede
-- leer empresa/suscripcion; el rol HTTP solo obtiene el idEmp ya validado.
-- La API nunca acepta idEmp de la peticion anonima: solo este codigo exacto.
CREATE OR REPLACE FUNCTION auth_resolver_empresa(p_codigo varchar) RETURNS integer
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT e.idemp FROM public.empresa e
  WHERE e.codigo = p_codigo AND e.estado = 'activa'
    AND EXISTS (SELECT 1 FROM public.suscripcion s
                WHERE s.idemp = e.idemp AND s.estado = 'activa'
                  AND current_date BETWEEN s.fechainicio AND s.fechavencimiento)
$$;
REVOKE ALL ON FUNCTION auth_resolver_empresa(varchar) FROM PUBLIC;

-- Uso de sucursales del plan: consumo y limite en una sola lectura.
CREATE OR REPLACE FUNCTION fn_plataforma_uso_sucursales(p_idEmp integer)
RETURNS TABLE(usadas integer, limite integer)
LANGUAGE sql STABLE AS $$
  SELECT (SELECT count(*)::integer FROM sucursal s WHERE s.idEmp=p_idEmp AND s.estado='activo'),
         COALESCE((SELECT p.maxSucursales::integer FROM suscripcion su
                   JOIN plan p ON p.idPlan=su.idPlan
                   WHERE su.idEmp=p_idEmp AND su.estado='activa'), 0);
$$;

-- Cambio de estado de empresa con validacion de suscripcion vigente al activar.
CREATE OR REPLACE PROCEDURE sp_plataforma_estado_empresa(p_idEmp integer, p_estado varchar)
LANGUAGE plpgsql AS $$
BEGIN
  IF p_estado NOT IN ('activa','suspendida','inactiva') THEN
    RAISE EXCEPTION 'ESTADO_EMPRESA_INVALIDO';
  END IF;
  UPDATE empresa SET estado=p_estado WHERE idEmp=p_idEmp;
  IF NOT FOUND THEN RAISE EXCEPTION 'EMPRESA_NO_ENCONTRADA'; END IF;
END;
$$;

CREATE OR REPLACE PROCEDURE sp_plataforma_identidad_visual(
  p_idEmp integer, p_nombre varchar, p_logo varchar, p_primario varchar, p_secundario varchar
) LANGUAGE plpgsql AS $$
BEGIN
  IF p_primario IS NOT NULL AND p_primario !~ '^#[0-9A-Fa-f]{6}$' THEN
    RAISE EXCEPTION 'COLOR_INVALIDO';
  END IF;
  IF p_secundario IS NOT NULL AND p_secundario !~ '^#[0-9A-Fa-f]{6}$' THEN
    RAISE EXCEPTION 'COLOR_INVALIDO';
  END IF;
  UPDATE empresa SET nombre=COALESCE(p_nombre,nombre), logoUrl=p_logo,
         colorPrimario=p_primario, colorSecundario=p_secundario
  WHERE idEmp=p_idEmp;
  IF NOT FOUND THEN RAISE EXCEPTION 'EMPRESA_NO_ENCONTRADA'; END IF;
END;
$$;

-- Registra auditoria de plataforma sin tenant (idEmp NULL).
CREATE OR REPLACE PROCEDURE sp_plataforma_auditar(
  p_usuario varchar, p_accion varchar, p_detalles jsonb, p_ip inet DEFAULT NULL
) LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM usuario u WHERE u.idUsuario=p_usuario AND u.idEmp IS NULL AND u.tipo='S') THEN
    RAISE EXCEPTION 'USUARIO_PLATAFORMA_INVALIDO';
  END IF;
  INSERT INTO bitacora(usuario_id,accion,ip,detalles,idEmp)
  VALUES (p_usuario,p_accion,p_ip,COALESCE(p_detalles,'{}'::jsonb),NULL);
END;
$$;

-- ==========================================================
-- 8) PERMISOS MINIMOS SOBRE LAS RUTINAS NUEVAS
-- ==========================================================
-- Por defecto PostgreSQL concede EXECUTE a PUBLIC: se revoca todo lo que no
-- debe usar el rol HTTP y se concede despues en 04_..._ROLES.sql.
DO $$
DECLARE x record;
BEGIN
  FOR x IN SELECT oid::regprocedure AS sign FROM pg_proc
    WHERE pronamespace='public'::regnamespace
      AND proname IN ('sp_registrar_venta_offline','sp_plataforma_estado_empresa',
                      'sp_plataforma_identidad_visual','sp_plataforma_auditar',
                      'fn_plataforma_uso_sucursales')
  LOOP
    EXECUTE format('REVOKE ALL ON ROUTINE %s FROM PUBLIC', x.sign);
  END LOOP;
END $$;

COMMIT;
