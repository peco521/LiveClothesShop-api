-- FashionStore / Examen Parcial 2 - ICONIX
-- TRIGGERS, FUNCIONES Y PROCEDIMIENTOS, PARA EL ESQUEMA DEL ARCHIVO 01.
-- NO es migración. Ejecutar después de 01_FashionStore_SaaS_TABLAS.sql.
-- PROTECCIÓN: usar un rol DB de aplicación sin BYPASSRLS ni propiedad sobre tablas.
-- FastAPI debe autenticar al usuario, resolver idEmp desde la sesión y ejecutar
-- SET LOCAL app.idemp = '<idEmp VALIDADO>' en cada transacción del tenant.
-- Los procedimientos con p_idEmp necesitan CONTEXTO de tenant/autorización en el backend.
-- La habilitación de SaaS/provisión y las tareas globales requieren un rol separado
-- y privilegiado, nunca las credenciales públicas de la aplicación.
-- No hay triggers de descuento de stock por venta: la transacción central lo hace;
-- así se evita un descuento doble. La reposición por devolución SI usa trigger.

BEGIN;

CREATE OR REPLACE FUNCTION fn_idemp_actual() RETURNS integer
LANGUAGE sql STABLE AS $$
  SELECT NULLIF(current_setting('app.idemp', true), '')::integer;
$$;

-- Ejecutar este guard en operaciones del tenant: el valor recibido debe
-- coincidir con el contexto validado por el backend en la misma transacción.
CREATE OR REPLACE FUNCTION fn_exigir_tenant(p_idEmp integer) RETURNS void
LANGUAGE plpgsql STABLE AS $$
BEGIN
  IF p_idEmp IS NULL OR fn_idemp_actual() IS DISTINCT FROM p_idEmp THEN
    RAISE EXCEPTION 'Tenant no autorizado o contexto de tenant ausente';
  END IF;
END;
$$;

-- ==========================================================
-- 1) FUNCIONES DE SUSCRIPCION
-- ==========================================================
CREATE OR REPLACE FUNCTION fn_suscripcion_vigente(p_idEmp integer) RETURNS boolean AS $$
  SELECT EXISTS (
    SELECT 1 FROM suscripcion s JOIN empresa e ON e.idEmp = s.idEmp
    WHERE s.idEmp = p_idEmp AND s.estado = 'activa' AND e.estado = 'activa'
      AND current_date BETWEEN s.fechaInicio AND s.fechaVencimiento);
$$ LANGUAGE sql STABLE;

CREATE OR REPLACE FUNCTION fn_tiene_funcionalidad(p_idEmp integer, p_codigo varchar) RETURNS boolean AS $$
  SELECT fn_suscripcion_vigente(p_idEmp) AND EXISTS (
    SELECT 1 FROM suscripcion s JOIN plan_funcionalidad pf ON pf.idPlan = s.idPlan
    WHERE s.idEmp = p_idEmp AND s.estado = 'activa' AND pf.codigo = p_codigo);
$$ LANGUAGE sql STABLE;

CREATE OR REPLACE FUNCTION fn_max_sucursales(p_idEmp integer) RETURNS integer AS $$
  SELECT p.maxSucursales FROM suscripcion s JOIN plan p ON p.idPlan = s.idPlan
  WHERE s.idEmp = p_idEmp AND s.estado = 'activa';
$$ LANGUAGE sql STABLE;

-- ==========================================================
-- 2) TRIGGERS
-- ==========================================================

-- ---------- 2.1 Limite de sucursales activas segun el plan ----------
CREATE OR REPLACE FUNCTION fn_validar_limite_sucursales() RETURNS TRIGGER AS $$
DECLARE
  v_max integer;
  v_act integer;
BEGIN
  IF NEW.estado = 'activo' THEN
    PERFORM fn_exigir_tenant(NEW.idEmp);
    IF TG_OP = 'UPDATE' AND OLD.estado = 'activo' THEN
      RETURN NEW;
    END IF;
    PERFORM pg_advisory_xact_lock(1001, NEW.idEmp);   -- evita que dos altas simultaneas superen el limite
    IF NOT fn_suscripcion_vigente(NEW.idEmp) THEN
      RAISE EXCEPTION 'La empresa % no tiene una suscripción vigente', NEW.idEmp;
    END IF;
    v_max := fn_max_sucursales(NEW.idEmp);
    IF TG_OP = 'INSERT' THEN
      SELECT count(*) INTO v_act FROM sucursal
      WHERE idEmp = NEW.idEmp AND estado = 'activo';
    ELSE
      SELECT count(*) INTO v_act FROM sucursal
      WHERE idEmp = NEW.idEmp AND estado = 'activo' AND nro <> NEW.nro;
    END IF;
    IF v_act >= v_max THEN
      RAISE EXCEPTION 'Límite de sucursales activas del plan alcanzado (% de %)', v_act, v_max;
    END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_limite_sucursales ON sucursal;
CREATE TRIGGER trg_limite_sucursales
BEFORE INSERT OR UPDATE OF estado ON sucursal
FOR EACH ROW EXECUTE FUNCTION fn_validar_limite_sucursales();

-- ---------- 2.2 Operaciones que exigen suscripcion vigente y funcionalidad habilitada ----------
CREATE OR REPLACE FUNCTION fn_exigir_funcionalidad() RETURNS TRIGGER AS $$
BEGIN
  PERFORM fn_exigir_tenant(NEW.idEmp);
  IF NOT fn_tiene_funcionalidad(NEW.idEmp, TG_ARGV[0]) THEN
    RAISE EXCEPTION 'La suscripción de la empresa % no está vigente o su plan no habilita %', NEW.idEmp, TG_ARGV[0];
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_func_venta ON venta;
CREATE TRIGGER trg_func_venta BEFORE INSERT ON venta
FOR EACH ROW EXECUTE FUNCTION fn_exigir_funcionalidad('VENTAS');

DROP TRIGGER IF EXISTS trg_func_reserva ON reserva;
CREATE TRIGGER trg_func_reserva BEFORE INSERT ON reserva
FOR EACH ROW EXECUTE FUNCTION fn_exigir_funcionalidad('RESERVAS');

DROP TRIGGER IF EXISTS trg_func_producto ON producto;
CREATE TRIGGER trg_func_producto BEFORE INSERT ON producto
FOR EACH ROW EXECUTE FUNCTION fn_exigir_funcionalidad('PRODUCTOS');

DROP TRIGGER IF EXISTS trg_func_cambio ON cambio;
CREATE TRIGGER trg_func_cambio BEFORE INSERT ON cambio
FOR EACH ROW EXECUTE FUNCTION fn_exigir_funcionalidad('VENTAS');

-- NO bloquear el registro de intentos fallidos en operacion_sync:
-- también debe auditarse una operación rechazada por suscripción vencida.
-- La habilitación OFFLINE se comprueba ANTES de confirmar la venta/reserva.

DROP TRIGGER IF EXISTS trg_func_reporte_ia ON reporte_solicitud;
CREATE TRIGGER trg_func_reporte_ia BEFORE INSERT ON reporte_solicitud
FOR EACH ROW EXECUTE FUNCTION fn_exigir_funcionalidad('REPORTES_IA');

-- ---------- 2.3 El tenant de una fila nunca puede cambiar ----------
CREATE OR REPLACE FUNCTION fn_idemp_inmutable() RETURNS TRIGGER AS $$
BEGIN
  IF NEW.idEmp IS DISTINCT FROM OLD.idEmp THEN
    RAISE EXCEPTION 'No se permite cambiar la empresa (idEmp) de un registro de %', TG_TABLE_NAME;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['empresa','ciudad','sucursal','rol','rol_funcion','usuario','bitacora','admin','empleado','cliente',
    'horario_trab','horario_emp','horario_atencion','horario_suc','talla','color','varianteColors','categoria',
    'marca','coleccion','temporada','tempColeccion','proveedor','promocion','producto','varianteProd',
    'inventario','movimiento_inv','carrito','detalleCarro','reserva','detalleReserva','venta','detalleVenta',
    'devolucion','detalleDev','pago','reembolso','cambio','detalleCambio','operacion_sync','reporte_solicitud',
    'politica_devolucion','recuperacion_contrasena']
  LOOP
    EXECUTE format('DROP TRIGGER IF EXISTS trg_idemp_inmutable ON %s', t);
    EXECUTE format('CREATE TRIGGER trg_idemp_inmutable BEFORE UPDATE OF idEmp ON %s
                    FOR EACH ROW EXECUTE FUNCTION fn_idemp_inmutable()', t);
  END LOOP;
END $$;

-- ---------- 2.4 Plazo para devoluciones y cambios (configurable por empresa) ----------
CREATE OR REPLACE FUNCTION fn_validar_plazo() RETURNS TRIGGER AS $$
DECLARE
  v_fecha timestamp;
  v_dias  integer;
BEGIN
  SELECT v.fechaHora,
         CASE TG_TABLE_NAME WHEN 'cambio' THEN e.diasPlazoCambio ELSE e.diasPlazoDevolucion END
    INTO v_fecha, v_dias
  FROM venta v JOIN empresa e ON e.idEmp = v.idEmp
  WHERE v.idEmp = NEW.idEmp AND v.nroVenta = NEW.nroVenta;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'La venta % no existe en la empresa %', NEW.nroVenta, NEW.idEmp;
  END IF;
  IF localtimestamp > v_fecha + make_interval(days => v_dias) THEN
    RAISE EXCEPTION 'La solicitud está fuera del plazo permitido (% días desde la venta)', v_dias;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- La devolución se valida por cada prenda en detalleDev:
-- diferentes productos pueden tener distintos plazos.
DROP TRIGGER IF EXISTS trg_plazo_devolucion ON devolucion;

DROP TRIGGER IF EXISTS trg_plazo_cambio ON cambio;
CREATE TRIGGER trg_plazo_cambio BEFORE INSERT ON cambio
FOR EACH ROW EXECUTE FUNCTION fn_validar_plazo();

-- ---------- 2.4.1 Política por producto/variante en detalleDev ----------
-- Política aplicable: idvar NULL (todo el producto) o idvar = variante vendida.
-- Si no hay política aplicable, se usa el plazo general de empresa.
CREATE OR REPLACE FUNCTION fn_validar_politica_detalle_devolucion()
RETURNS TRIGGER AS $$
DECLARE
  v_fechaVenta timestamp;
  v_dias integer;
  v_pct numeric(5,2);
  v_ventaCab integer;
BEGIN
  SELECT d.nroVenta, v.fechaHora,
         COALESCE(dv.diasDevolucionAplicado,pd.dias,e.diasPlazoDevolucion)::integer,
         COALESCE(dv.porcentajeDevolucionAplicado,pd.porcentaje,e.porcentajeDevolucion)::numeric(5,2)
    INTO v_ventaCab, v_fechaVenta, v_dias, v_pct
  FROM devolucion d
  JOIN venta v ON v.idEmp = d.idEmp AND v.nroVenta = d.nroVenta
  JOIN empresa e ON e.idEmp = d.idEmp
  JOIN detalleVenta dv ON dv.idEmp = d.idEmp
                      AND dv.nroVenta = NEW.nroVenta
                      AND dv.idDetalleVenta = NEW.idDetalleVenta
  JOIN varianteProd vp ON vp.idEmp = dv.idEmp AND vp.idVariante = dv.idVar
  LEFT JOIN politica_devolucion pd
    ON pd.idEmp = vp.idEmp
   AND pd.idprod = vp.idProd
   AND (pd.idvar IS NULL OR pd.idvar = vp.idVariante)
  WHERE d.idEmp = NEW.idEmp AND d.nroDev = NEW.nroDev;

  IF NOT FOUND OR v_ventaCab IS DISTINCT FROM NEW.nroVenta THEN
    RAISE EXCEPTION 'Detalle de devolución incompatible con la venta original';
  END IF;

  IF localtimestamp > v_fechaVenta + make_interval(days => v_dias) THEN
    RAISE EXCEPTION
      'Fuera de plazo: política para esta prenda permite % días (reembolso máximo %)',
      v_dias, v_pct;
  END IF;

  -- Evitar doble devolución/cambio, incluso si se inserta directamente en detalleDev.
  PERFORM 1 FROM detalleVenta dv
  WHERE dv.idEmp=NEW.idEmp AND dv.nroVenta=NEW.nroVenta
    AND dv.idDetalleVenta=NEW.idDetalleVenta FOR UPDATE;
  IF NEW.cantidad > (
    SELECT dv.cantidad
      - COALESCE((SELECT sum(dd.cantidad) FROM detalleDev dd
          JOIN devolucion d ON d.idEmp=dd.idEmp AND d.nroDev=dd.nroDev
          WHERE dd.idEmp=NEW.idEmp AND dd.nroVenta=NEW.nroVenta
            AND dd.idDetalleVenta=NEW.idDetalleVenta AND d.estado<>'rechazada'),0)
      - COALESCE((SELECT sum(dc.cantidad) FROM detalleCambio dc
          JOIN cambio c ON c.idEmp=dc.idEmp AND c.nroCambio=dc.nroCambio
          WHERE dc.idEmp=NEW.idEmp AND dc.nroVenta=NEW.nroVenta
            AND dc.idDetalleVenta=NEW.idDetalleVenta AND c.estado<>'rechazado'),0)
    FROM detalleVenta dv WHERE dv.idEmp=NEW.idEmp AND dv.nroVenta=NEW.nroVenta
      AND dv.idDetalleVenta=NEW.idDetalleVenta
  ) THEN
    RAISE EXCEPTION 'La linea vendida ya fue devuelta o cambiada total/parcialmente';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_validar_politica_detalle_devolucion ON detalleDev;
CREATE TRIGGER trg_validar_politica_detalle_devolucion
BEFORE INSERT ON detalleDev
FOR EACH ROW EXECUTE FUNCTION fn_validar_politica_detalle_devolucion();

-- ---------- 2.4.2 Congelar política aplicable al momento de venta ----------
-- La empresa puede actualizar sus políticas después sin modificar ventas pasadas.
CREATE OR REPLACE FUNCTION fn_snapshot_politica_venta() RETURNS TRIGGER AS $$
DECLARE
  v_dias integer;
  v_porcentaje numeric(5,2);
  v_condiciones text;
BEGIN
  SELECT COALESCE(pd.dias,e.diasPlazoDevolucion),
         COALESCE(pd.porcentaje,e.porcentajeDevolucion),
         e.condicionesDevolucion
    INTO v_dias,v_porcentaje,v_condiciones
  FROM varianteProd vp
  JOIN empresa e ON e.idEmp=vp.idEmp
  LEFT JOIN politica_devolucion pd
    ON pd.idEmp=vp.idEmp AND pd.idprod=vp.idProd
   AND (pd.idvar IS NULL OR pd.idvar=vp.idVariante)
  WHERE vp.idEmp=NEW.idEmp AND vp.idVariante=NEW.idVar;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'Variante inexistente para calcular politica de devolucion';
  END IF;
  NEW.diasDevolucionAplicado := v_dias;
  NEW.porcentajeDevolucionAplicado := v_porcentaje;
  NEW.condicionesDevolucionAplicadas := v_condiciones;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_snapshot_politica_venta
BEFORE INSERT ON detalleVenta FOR EACH ROW
EXECUTE FUNCTION fn_snapshot_politica_venta();

CREATE OR REPLACE FUNCTION fn_proteger_snapshot_politica() RETURNS TRIGGER AS $$
BEGIN
  IF NEW.diasDevolucionAplicado IS DISTINCT FROM OLD.diasDevolucionAplicado
     OR NEW.porcentajeDevolucionAplicado IS DISTINCT FROM OLD.porcentajeDevolucionAplicado
     OR NEW.condicionesDevolucionAplicadas IS DISTINCT FROM OLD.condicionesDevolucionAplicadas THEN
    RAISE EXCEPTION 'No se pueden alterar las políticas históricas de una venta';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER trg_snapshot_politica_inmutable
BEFORE UPDATE ON detalleVenta FOR EACH ROW
EXECUTE FUNCTION fn_proteger_snapshot_politica();

-- ---------- 2.5 Reponer stock al aprobar una devolucion ----------
CREATE OR REPLACE FUNCTION fn_reponer_stock_devolucion() RETURNS TRIGGER AS $$
DECLARE
  v_nroSuc INTEGER;
  r RECORD;
  v_nroInv INTEGER;
BEGIN
  IF NEW.estado = 'aprobada' AND OLD.estado <> 'aprobada' THEN
    SELECT nroSuc INTO v_nroSuc FROM venta WHERE idEmp = NEW.idEmp AND nroVenta = NEW.nroVenta;

    FOR r IN
      SELECT dd.cantidad, dv.idVar
      FROM detalleDev dd
      JOIN detalleVenta dv ON dv.idEmp = dd.idEmp AND dv.nroVenta = dd.nroVenta
                          AND dv.idDetalleVenta = dd.idDetalleVenta
      WHERE dd.idEmp = NEW.idEmp AND dd.nroDev = NEW.nroDev
    LOOP
      SELECT nroInv INTO v_nroInv FROM inventario
      WHERE idEmp = NEW.idEmp AND nroSuc = v_nroSuc AND idVar = r.idVar;
      IF v_nroInv IS NULL THEN
        RAISE EXCEPTION 'No existe inventario de la variante % en la sucursal %', r.idVar, v_nroSuc;
      END IF;

      UPDATE inventario SET stock = stock + r.cantidad, cantDisp = cantDisp + r.cantidad
      WHERE nroInv = v_nroInv AND idEmp = NEW.idEmp;

      INSERT INTO movimiento_inv (idEmp, tipoMov, cantidad, fecha, motivo, nroInv)
      VALUES (NEW.idEmp, 'entrada', r.cantidad, CURRENT_DATE, 'Devolución #' || NEW.nroDev, v_nroInv);
    END LOOP;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_reponer_stock_devolucion ON devolucion;
CREATE TRIGGER trg_reponer_stock_devolucion
AFTER UPDATE ON devolucion
FOR EACH ROW EXECUTE FUNCTION fn_reponer_stock_devolucion();

-- ---------- 2.6 Cambios de prendas: validar linea y ajustar stock al aprobar ----------
CREATE OR REPLACE FUNCTION fn_validar_detalle_cambio() RETURNS TRIGGER AS $$
DECLARE
  v_nroVenta integer;
  v_vendida  integer;
BEGIN
  SELECT nroVenta INTO v_nroVenta FROM cambio WHERE idEmp = NEW.idEmp AND nroCambio = NEW.nroCambio;
  IF v_nroVenta IS DISTINCT FROM NEW.nroVenta THEN
    RAISE EXCEPTION 'La prenda devuelta debe pertenecer a la venta % del cambio', v_nroVenta;
  END IF;
  SELECT cantidad INTO v_vendida FROM detalleVenta
  WHERE idEmp = NEW.idEmp AND nroVenta = NEW.nroVenta AND idDetalleVenta = NEW.idDetalleVenta
  FOR UPDATE;
  SELECT v_vendida
      - COALESCE((SELECT sum(dd.cantidad) FROM detalleDev dd
          JOIN devolucion d ON d.idEmp=dd.idEmp AND d.nroDev=dd.nroDev
          WHERE dd.idEmp=NEW.idEmp AND dd.nroVenta=NEW.nroVenta
            AND dd.idDetalleVenta=NEW.idDetalleVenta AND d.estado<>'rechazada'),0)
      - COALESCE((SELECT sum(dc.cantidad) FROM detalleCambio dc
          JOIN cambio c ON c.idEmp=dc.idEmp AND c.nroCambio=dc.nroCambio
          WHERE dc.idEmp=NEW.idEmp AND dc.nroVenta=NEW.nroVenta
            AND dc.idDetalleVenta=NEW.idDetalleVenta AND c.estado<>'rechazado'),0)
    INTO v_vendida;
  IF v_vendida IS NULL OR NEW.cantidad > v_vendida THEN
    RAISE EXCEPTION 'La linea % no tiene unidades suficientes para cambiar', NEW.idDetalleVenta;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_validar_detalle_cambio ON detalleCambio;
CREATE TRIGGER trg_validar_detalle_cambio BEFORE INSERT ON detalleCambio
FOR EACH ROW EXECUTE FUNCTION fn_validar_detalle_cambio();

CREATE OR REPLACE FUNCTION fn_ajustar_stock_cambio() RETURNS TRIGGER AS $$
DECLARE
  r RECORD;
  v_inv_in  integer;
  v_inv_out integer;
BEGIN
  IF NEW.estado = 'aprobado' AND OLD.estado <> 'aprobado' THEN
    FOR r IN
      SELECT dc.cantidad, dv.idVar AS idVarVieja, dc.idVarNueva
      FROM detalleCambio dc
      JOIN detalleVenta dv ON dv.idEmp = dc.idEmp AND dv.nroVenta = dc.nroVenta
                          AND dv.idDetalleVenta = dc.idDetalleVenta
      WHERE dc.idEmp = NEW.idEmp AND dc.nroCambio = NEW.nroCambio
    LOOP
      SELECT nroInv INTO v_inv_in  FROM inventario WHERE idEmp = NEW.idEmp AND nroSuc = NEW.nroSuc AND idVar = r.idVarVieja;
      SELECT nroInv INTO v_inv_out FROM inventario WHERE idEmp = NEW.idEmp AND nroSuc = NEW.nroSuc AND idVar = r.idVarNueva;
      IF v_inv_in IS NULL OR v_inv_out IS NULL THEN
        RAISE EXCEPTION 'Falta inventario en la sucursal % para completar el cambio %', NEW.nroSuc, NEW.nroCambio;
      END IF;

      UPDATE inventario SET stock = stock + r.cantidad, cantDisp = cantDisp + r.cantidad WHERE nroInv = v_inv_in;
      UPDATE inventario SET stock = stock - r.cantidad, cantDisp = cantDisp - r.cantidad WHERE nroInv = v_inv_out;

      INSERT INTO movimiento_inv (idEmp, tipoMov, cantidad, fecha, motivo, nroInv) VALUES
        (NEW.idEmp, 'entrada', r.cantidad, CURRENT_DATE, 'Cambio #' || NEW.nroCambio || ' (prenda devuelta)', v_inv_in),
        (NEW.idEmp, 'salida',  r.cantidad, CURRENT_DATE, 'Cambio #' || NEW.nroCambio || ' (prenda entregada)', v_inv_out);
    END LOOP;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_ajustar_stock_cambio ON cambio;
CREATE TRIGGER trg_ajustar_stock_cambio AFTER UPDATE ON cambio
FOR EACH ROW EXECUTE FUNCTION fn_ajustar_stock_cambio();

-- (La reserva/liberacion de disponibilidad por venta, la conversion del carrito y la bitacora
--  siguen siendo responsabilidad del backend, como en el Parcial 1.)

-- ==========================================================
-- 3) PROCEDIMIENTOS DE PLATAFORMA SaaS
-- ==========================================================

-- ---------- 3.1 Registrar empresa + suscripcion + roles base + administrador principal ----------
CREATE OR REPLACE PROCEDURE sp_registrar_empresa(
  p_codigo VARCHAR, p_nombre VARCHAR, p_nit VARCHAR, p_idPlan SMALLINT,
  p_fechaInicio DATE, p_fechaVencimiento DATE,
  p_idAdmin VARCHAR, p_ci VARCHAR, p_nombreAdmin VARCHAR, p_apellidoPat VARCHAR, p_apellidoMat VARCHAR,
  p_sexo CHAR, p_correo VARCHAR, p_telefono VARCHAR, p_direccion VARCHAR,
  p_contrasena_hash VARCHAR, p_fechaNac DATE,
  INOUT p_idEmp INTEGER DEFAULT NULL
)
LANGUAGE plpgsql
AS $$
BEGIN
  IF p_contrasena_hash NOT LIKE '$argon2id$%' THEN
    RAISE EXCEPTION 'La contraseña debe recibirse como hash Argon2id';
  END IF;

  INSERT INTO empresa (codigo, nombre, nit) VALUES (lower(btrim(p_codigo)), p_nombre, p_nit)
  RETURNING idEmp INTO p_idEmp;

  INSERT INTO suscripcion (idEmp, idPlan, fechaInicio, fechaVencimiento)
  VALUES (p_idEmp, p_idPlan, p_fechaInicio, p_fechaVencimiento);

  INSERT INTO rol (nro, descripcion, idEmp) VALUES
    ('E' || p_idEmp || '-ADM', 'Administrador',          p_idEmp),
    ('E' || p_idEmp || '-ENC', 'Encargado de sucursal',  p_idEmp),
    ('E' || p_idEmp || '-CAJ', 'Cajero',                 p_idEmp),
    ('E' || p_idEmp || '-CLI', 'Cliente',                p_idEmp);

  INSERT INTO usuario (idUsuario, ci, nombre, apellidoPat, apellidoMat, sexo, correo, telefono,
                       direccion, contrasena, fechaNac, tipo, nroRol, idEmp)
  VALUES (p_idAdmin, p_ci, btrim(p_nombreAdmin), p_apellidoPat, p_apellidoMat, p_sexo,
          lower(btrim(p_correo)), p_telefono, p_direccion, p_contrasena_hash, p_fechaNac,
          'A', 'E' || p_idEmp || '-ADM', p_idEmp);

  INSERT INTO admin (idUsuario, cod_adm, principal, idEmp) VALUES (p_idAdmin, 'ADM-' || p_idEmp, true, p_idEmp);
END;
$$;

-- ---------- 3.2 Cambiar / renovar plan ----------
CREATE OR REPLACE PROCEDURE sp_cambiar_plan(
  p_idEmp INTEGER, p_idPlan SMALLINT, p_fechaInicio DATE, p_fechaVencimiento DATE
)
LANGUAGE plpgsql
AS $$
DECLARE
  v_max integer;
  v_act integer;
BEGIN
  SELECT maxSucursales INTO v_max FROM plan WHERE idPlan = p_idPlan AND estado = 'activo';
  IF NOT FOUND THEN
    RAISE EXCEPTION 'El plan % no existe o no está activo', p_idPlan;
  END IF;

  PERFORM pg_advisory_xact_lock(1001, p_idEmp);
  SELECT count(*) INTO v_act FROM sucursal WHERE idEmp = p_idEmp AND estado = 'activo';
  IF v_act > v_max THEN
    RAISE EXCEPTION 'La empresa tiene % sucursales activas y el plan solicitado solo permite %', v_act, v_max;
  END IF;

  UPDATE suscripcion SET estado = 'reemplazada' WHERE idEmp = p_idEmp AND estado = 'activa';
  INSERT INTO suscripcion (idEmp, idPlan, fechaInicio, fechaVencimiento)
  VALUES (p_idEmp, p_idPlan, p_fechaInicio, p_fechaVencimiento);
END;
$$;

-- ---------- 3.3 Marcar suscripciones vencidas (tarea periodica) ----------
CREATE OR REPLACE PROCEDURE sp_actualizar_suscripciones_vencidas()
LANGUAGE plpgsql
AS $$
BEGIN
  UPDATE suscripcion SET estado = 'vencida'
  WHERE estado = 'activa' AND fechaVencimiento < current_date;
END;
$$;

-- ==========================================================
-- 4) PROCEDIMIENTOS DE NEGOCIO (ahora con p_idEmp)
-- ==========================================================

-- ---------- 4.1 Registrarse (Cliente) ----------
CREATE OR REPLACE PROCEDURE sp_registrar_cliente(
  p_idEmp INTEGER,
  p_idUsuario VARCHAR, p_ci VARCHAR, p_nombre VARCHAR,
  p_apellidoPat VARCHAR, p_apellidoMat VARCHAR, p_sexo CHAR,
  p_correo VARCHAR, p_telefono VARCHAR, p_direccion VARCHAR,
  p_contrasena_hash VARCHAR, p_fechaNac DATE, p_nroRol VARCHAR, p_cod_cl VARCHAR
)
LANGUAGE plpgsql
AS $$
BEGIN
  PERFORM fn_exigir_tenant(p_idEmp);
  IF p_nombre IS NULL OR btrim(p_nombre) = '' THEN
    RAISE EXCEPTION 'nombre es obligatorio';
  END IF;
  IF p_contrasena_hash NOT LIKE '$argon2id$%' THEN
    RAISE EXCEPTION 'La contraseña debe recibirse como hash Argon2id';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM empresa WHERE idEmp = p_idEmp AND estado = 'activa') THEN
    RAISE EXCEPTION 'La empresa % no existe o no está activa', p_idEmp;
  END IF;

  INSERT INTO usuario (
    idEmp, idUsuario, ci, nombre, apellidoPat, apellidoMat, sexo, correo, telefono,
    direccion, contrasena, fechaNac, tipo, nroRol
  ) VALUES (
    p_idEmp, p_idUsuario, p_ci, btrim(p_nombre), p_apellidoPat, p_apellidoMat, p_sexo,
    lower(btrim(p_correo)), p_telefono, p_direccion, p_contrasena_hash,
    p_fechaNac, 'C', p_nroRol
  );

  INSERT INTO cliente (idEmp, idUsuario, cod_cl) VALUES (p_idEmp, p_idUsuario, p_cod_cl);
END;
$$;

-- ---------- 4.2 Recuperar Contraseña ----------
-- p_idEmp NULL = usuario de plataforma
CREATE OR REPLACE PROCEDURE sp_cambiar_contrasena(p_idEmp INTEGER, p_correo VARCHAR, p_nueva_contrasena VARCHAR)
LANGUAGE plpgsql
AS $$
BEGIN
  -- La cuenta SaaS (idEmp NULL) usa flujo separado, bajo rol privilegiado.
  PERFORM fn_exigir_tenant(p_idEmp);
  IF p_nueva_contrasena NOT LIKE '$argon2id$%' THEN
    RAISE EXCEPTION 'La contraseña debe recibirse como hash Argon2id';
  END IF;
  UPDATE usuario
  SET contrasena = p_nueva_contrasena
  WHERE idEmp IS NOT DISTINCT FROM p_idEmp
    AND lower(btrim(correo)) = lower(btrim(p_correo));
  IF NOT FOUND THEN
    RAISE EXCEPTION 'No existe un usuario con el correo % en la empresa indicada', p_correo;
  END IF;
END;
$$;

-- ---------- 4.3 Agregar Productos al Carrito ----------
CREATE OR REPLACE PROCEDURE sp_agregar_al_carrito(
  p_idEmp INTEGER, p_idUsuarioCl VARCHAR, p_idVar VARCHAR, p_cantidad INTEGER
)
LANGUAGE plpgsql
AS $$
DECLARE
  v_idCarrito INTEGER;
  v_disponible INTEGER;
  v_cantidad_actual INTEGER;
BEGIN
  PERFORM fn_exigir_tenant(p_idEmp);
  IF p_cantidad IS NULL OR p_cantidad <= 0 THEN
    RAISE EXCEPTION 'La cantidad debe ser mayor que cero';
  END IF;

  PERFORM 1 FROM cliente WHERE idUsuario = p_idUsuarioCl AND idEmp = p_idEmp FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'El cliente % no existe en la empresa %', p_idUsuarioCl, p_idEmp;
  END IF;

  SELECT idCarrito INTO v_idCarrito
  FROM carrito
  WHERE idUsuarioCl = p_idUsuarioCl AND idEmp = p_idEmp AND estado = 'activo'
  FOR UPDATE;

  IF v_idCarrito IS NULL THEN
    INSERT INTO carrito (idEmp, idUsuarioCl, estado)
    VALUES (p_idEmp, p_idUsuarioCl, 'activo')
    RETURNING idCarrito INTO v_idCarrito;
  END IF;

  SELECT COALESCE(SUM(cantDisp), 0)::INTEGER INTO v_disponible
  FROM inventario WHERE idVar = p_idVar AND idEmp = p_idEmp;

  SELECT cantidad INTO v_cantidad_actual
  FROM detalleCarro
  WHERE idCarrito = v_idCarrito AND idVar = p_idVar AND idEmp = p_idEmp
  FOR UPDATE;

  IF COALESCE(v_cantidad_actual, 0) + p_cantidad > v_disponible THEN
    RAISE EXCEPTION 'No hay suficiente disponibilidad para la variante %', p_idVar;
  END IF;

  IF v_cantidad_actual IS NULL THEN
    INSERT INTO detalleCarro (idEmp, idCarrito, idDetalleCarro, idVar, cantidad)
    VALUES (
      p_idEmp, v_idCarrito,
      COALESCE((SELECT MAX(idDetalleCarro) FROM detalleCarro
                WHERE idCarrito = v_idCarrito AND idEmp = p_idEmp), 0) + 1,
      p_idVar, p_cantidad
    );
  ELSE
    UPDATE detalleCarro
    SET cantidad = v_cantidad_actual + p_cantidad
    WHERE idCarrito = v_idCarrito AND idVar = p_idVar AND idEmp = p_idEmp;
  END IF;
END;
$$;

-- ---------- 4.4 Gestionar Reservas: crear (idempotente si llega p_idOperacion) ----------
CREATE OR REPLACE PROCEDURE sp_realizar_reserva(
  p_idEmp INTEGER, p_idUsuarioCl VARCHAR, p_nroSuc INTEGER, p_fechaReserva DATE, p_horaAtencion TIME,
  p_idVars VARCHAR[], p_cantidades INTEGER[],
  INOUT p_nroReserva INTEGER DEFAULT NULL,
  p_idOperacion UUID DEFAULT NULL, p_origen VARCHAR DEFAULT 'servidor'
)
LANGUAGE plpgsql
AS $$
DECLARE
  i INTEGER;
  v_disponible INTEGER;
BEGIN
  PERFORM fn_exigir_tenant(p_idEmp);
  IF p_origen <> 'servidor' AND NOT fn_tiene_funcionalidad(p_idEmp,'OFFLINE') THEN
    RAISE EXCEPTION 'La suscripcion no habilita el registro offline';
  END IF;
  -- Serializa reintentos concurrentes del mismo UUID antes del SELECT.
  IF p_idOperacion IS NOT NULL THEN
    PERFORM pg_advisory_xact_lock(p_idEmp, hashtext(p_idOperacion::text));
  END IF;
  -- Reenvio de una operacion offline ya confirmada: devuelve la reserva existente, no duplica
  IF p_idOperacion IS NOT NULL THEN
    SELECT nroReserva INTO p_nroReserva FROM reserva
    WHERE idEmp = p_idEmp AND idOperacion = p_idOperacion;
    IF FOUND THEN
      RETURN;
    END IF;
  END IF;

  IF array_length(p_idVars,1) IS NULL OR array_length(p_idVars,1) <> array_length(p_cantidades,1) THEN
    RAISE EXCEPTION 'Debe indicar al menos una prenda con su cantidad correspondiente';
  END IF;

  -- Una misma variante repetida en el arreglo puede evadir la comprobación
  -- individual de disponibilidad. Exigir IDs distintos.
  IF (SELECT count(*) FROM unnest(p_idVars) AS x(idvar))
     <> (SELECT count(DISTINCT idvar) FROM unnest(p_idVars) AS x(idvar)) THEN
    RAISE EXCEPTION 'No se debe repetir una variante en la misma reserva';
  END IF;

  PERFORM 1 FROM sucursal WHERE nro = p_nroSuc AND idEmp = p_idEmp AND estado = 'activo';
  IF NOT FOUND THEN
    RAISE EXCEPTION 'La sucursal % no existe, no está activa o no pertenece a la empresa %', p_nroSuc, p_idEmp;
  END IF;

  FOR i IN 1 .. array_length(p_idVars,1) LOOP
    IF p_idVars[i] IS NULL OR btrim(p_idVars[i]) = ''
       OR p_cantidades[i] IS NULL OR p_cantidades[i] <= 0 THEN
      RAISE EXCEPTION 'Variante o cantidad inválida en la posición %', i;
    END IF;

    SELECT cantDisp INTO v_disponible
    FROM inventario
    WHERE nroSuc = p_nroSuc AND idVar = p_idVars[i] AND idEmp = p_idEmp
    FOR UPDATE;

    IF NOT FOUND OR v_disponible < p_cantidades[i] THEN
      RAISE EXCEPTION 'No hay disponibilidad para la variante % en la sucursal %',
        p_idVars[i], p_nroSuc;
    END IF;
  END LOOP;

  INSERT INTO reserva (idEmp, fechaReserva, horaAtencion, estado, nroSuc, idUsuarioCl,
                       idOperacion, origen, fechaSync)
  VALUES (p_idEmp, p_fechaReserva, p_horaAtencion, 'pendiente', p_nroSuc, p_idUsuarioCl,
          p_idOperacion, p_origen, CASE WHEN p_origen <> 'servidor' THEN now() END)
  RETURNING nroReserva INTO p_nroReserva;

  FOR i IN 1 .. array_length(p_idVars,1) LOOP
    UPDATE inventario
    SET cantDisp = cantDisp - p_cantidades[i]
    WHERE nroSuc = p_nroSuc AND idVar = p_idVars[i] AND idEmp = p_idEmp;

    INSERT INTO detalleReserva (idEmp, nroReserva, idDetalleRes, idVar, cantidad)
    VALUES (p_idEmp, p_nroReserva, i, p_idVars[i], p_cantidades[i]);
  END LOOP;
END;
$$;

-- ---------- 4.5 Gestionar Reservas: cancelar ----------
CREATE OR REPLACE PROCEDURE sp_cancelar_reserva(p_idEmp INTEGER, p_nroReserva INTEGER)
LANGUAGE plpgsql
AS $$
DECLARE
  v_nroSuc INTEGER;
  v_estado VARCHAR;
BEGIN
  PERFORM fn_exigir_tenant(p_idEmp);
  SELECT nroSuc, estado INTO v_nroSuc, v_estado
  FROM reserva
  WHERE nroReserva = p_nroReserva AND idEmp = p_idEmp
  FOR UPDATE;

  IF NOT FOUND OR v_estado NOT IN ('pendiente', 'confirmada') THEN
    RAISE EXCEPTION 'La reserva % no existe o ya no puede cancelarse', p_nroReserva;
  END IF;

  UPDATE inventario inv
  SET cantDisp = inv.cantDisp + liberacion.cantidad
  FROM (
    SELECT idVar, sum(cantidad)::integer AS cantidad
    FROM detalleReserva
    WHERE nroReserva = p_nroReserva AND idEmp = p_idEmp
    GROUP BY idVar
  ) AS liberacion
  WHERE inv.nroSuc = v_nroSuc AND inv.idVar = liberacion.idVar AND inv.idEmp = p_idEmp;

  UPDATE reserva SET estado = 'cancelada' WHERE nroReserva = p_nroReserva AND idEmp = p_idEmp;
END;
$$;

-- ---------- 4.6 Gestionar Reservas: vencimiento automatico (todas las empresas) ----------
-- Sin parametro usa el margen configurado en empresa.horasMargenReserva.
CREATE OR REPLACE PROCEDURE sp_marcar_reservas_vencidas(p_horas_margen INTEGER DEFAULT NULL)
LANGUAGE plpgsql
AS $$
BEGIN
  IF p_horas_margen < 0 THEN
    RAISE EXCEPTION 'El margen no puede ser negativo';
  END IF;

  WITH vencidas AS (
    UPDATE reserva r
    SET estado = 'vencida'
    FROM empresa e
    WHERE e.idEmp = r.idEmp
      AND r.estado IN ('pendiente', 'confirmada')
      AND (r.fechaReserva + r.horaAtencion
           + make_interval(hours => COALESCE(p_horas_margen, e.horasMargenReserva::integer))) < now()
    RETURNING r.nroReserva, r.nroSuc, r.idEmp
  ), liberacion AS (
    SELECT v.idEmp, v.nroSuc, d.idVar, sum(d.cantidad)::integer AS cantidad
    FROM vencidas v
    JOIN detalleReserva d ON d.nroReserva = v.nroReserva AND d.idEmp = v.idEmp
    GROUP BY v.idEmp, v.nroSuc, d.idVar
  )
  UPDATE inventario inv
  SET cantDisp = inv.cantDisp + liberacion.cantidad
  FROM liberacion
  WHERE inv.idEmp = liberacion.idEmp AND inv.nroSuc = liberacion.nroSuc AND inv.idVar = liberacion.idVar;
END;
$$;

-- ---------- 4.7 Registrar Devolucion de Prendas ----------
CREATE OR REPLACE PROCEDURE sp_registrar_devolucion(
  p_idEmp INTEGER, p_nroVenta INTEGER, p_monto NUMERIC,
  p_idDetalleVentas INTEGER[], p_cantidades INTEGER[],
  INOUT p_nroDev INTEGER DEFAULT NULL
)
LANGUAGE plpgsql
AS $$
DECLARE
  i INTEGER;
  v_disponible INTEGER;
  v_monto_maximo numeric(12,2);
BEGIN
  PERFORM fn_exigir_tenant(p_idEmp);
  IF array_length(p_idDetalleVentas,1) IS NULL
     OR array_length(p_idDetalleVentas,1) <> array_length(p_cantidades,1) THEN
    RAISE EXCEPTION 'Debe indicar al menos una prenda con su cantidad correspondiente';
  END IF;

  IF (SELECT count(*) FROM unnest(p_idDetalleVentas) AS x(iddet))
     <> (SELECT count(DISTINCT iddet) FROM unnest(p_idDetalleVentas) AS x(iddet)) THEN
    RAISE EXCEPTION 'No repita una línea de venta en la misma devolución';
  END IF;

  FOR i IN 1 .. array_length(p_cantidades, 1) LOOP
    IF p_cantidades[i] IS NULL OR p_cantidades[i] <= 0 THEN
      RAISE EXCEPTION 'La cantidad a devolver debe ser positiva';
    END IF;
  END LOOP;

  -- No se puede devolver mas de lo vendido (descontando devoluciones previas no rechazadas)
  FOR i IN 1 .. array_length(p_idDetalleVentas,1) LOOP
    SELECT dv.cantidad - COALESCE((
             SELECT sum(dd.cantidad) FROM detalleDev dd
             JOIN devolucion d ON d.idEmp = dd.idEmp AND d.nroDev = dd.nroDev
             WHERE dd.idEmp = p_idEmp AND dd.nroVenta = p_nroVenta
               AND dd.idDetalleVenta = p_idDetalleVentas[i] AND d.estado <> 'rechazada'), 0)
             - COALESCE((
               SELECT sum(dc.cantidad) FROM detalleCambio dc
               JOIN cambio c ON c.idEmp=dc.idEmp AND c.nroCambio=dc.nroCambio
               WHERE dc.idEmp=p_idEmp AND dc.nroVenta=p_nroVenta
                 AND dc.idDetalleVenta=p_idDetalleVentas[i] AND c.estado <> 'rechazado'),0)
      INTO v_disponible
    FROM detalleVenta dv
    WHERE dv.idEmp = p_idEmp AND dv.nroVenta = p_nroVenta AND dv.idDetalleVenta = p_idDetalleVentas[i]
    FOR UPDATE;

    IF v_disponible IS NULL OR p_cantidades[i] > v_disponible THEN
      RAISE EXCEPTION 'La línea % de la venta % no admite devolver % unidades', p_idDetalleVentas[i], p_nroVenta, p_cantidades[i];
    END IF;
  END LOOP;

  INSERT INTO devolucion (idEmp, monto, estado, nroVenta) VALUES (p_idEmp, p_monto, 'pendiente', p_nroVenta)
  RETURNING nroDev INTO p_nroDev;

  FOR i IN 1 .. array_length(p_idDetalleVentas,1) LOOP
    INSERT INTO detalleDev (idEmp, nroDev, idDetalleDev, cantidad, nroVenta, idDetalleVenta)
    VALUES (p_idEmp, p_nroDev, i, p_cantidades[i], p_nroVenta, p_idDetalleVentas[i]);
  END LOOP;

  -- Calcular techo de devolución aplicando porcentaje de la política.
  -- Si no existe política específica, aplicar el porcentaje de la empresa.
  -- IMPORTANTE: el backend debe considerar descuentos efectivamente cobrados;
  -- esto constituye un techo de seguridad, no un cálculo tributario final.
  SELECT COALESCE(
    SUM(ROUND(dd.cantidad * dv.precioUnitario
              * COALESCE(dv.porcentajeDevolucionAplicado,pd.porcentaje,e.porcentajeDevolucion)::numeric / 100, 2)), 0)
    INTO v_monto_maximo
  FROM detalleDev dd
  JOIN detalleVenta dv ON dv.idEmp=dd.idEmp
                      AND dv.nroVenta=dd.nroVenta
                      AND dv.idDetalleVenta=dd.idDetalleVenta
  JOIN varianteProd vp ON vp.idEmp=dv.idEmp AND vp.idVariante=dv.idVar
  JOIN empresa e ON e.idEmp=dd.idEmp
  LEFT JOIN politica_devolucion pd
    ON pd.idEmp=vp.idEmp AND pd.idprod=vp.idProd
   AND (pd.idvar IS NULL OR pd.idvar=vp.idVariante)
  WHERE dd.idEmp=p_idEmp AND dd.nroDev=p_nroDev;

  IF p_monto IS NULL OR p_monto < 0 OR p_monto > v_monto_maximo THEN
    RAISE EXCEPTION 'Monto de devolución % fuera del máximo permitido por políticas (%)',
      p_monto, v_monto_maximo;
  END IF;
END;
$$;

-- ---------- 4.8 Aprobar Devolucion (dispara reposicion de stock) ----------
CREATE OR REPLACE PROCEDURE sp_aprobar_devolucion(p_idEmp INTEGER, p_nroDev INTEGER)
LANGUAGE plpgsql
AS $$
BEGIN
  PERFORM fn_exigir_tenant(p_idEmp);
  UPDATE devolucion SET estado = 'aprobada'
  WHERE nroDev = p_nroDev AND idEmp = p_idEmp AND estado = 'pendiente';
  IF NOT FOUND THEN
    RAISE EXCEPTION 'La devolución % no existe o ya fue procesada', p_nroDev;
  END IF;
END;
$$;

-- ---------- 4.9 Procesar Reembolso ----------
CREATE OR REPLACE PROCEDURE sp_procesar_reembolso(
  p_idEmp INTEGER, p_nroDev INTEGER, p_idPago INTEGER, p_monto NUMERIC, p_metodo VARCHAR
)
LANGUAGE plpgsql
AS $$
DECLARE
  v_tope numeric(10,2);
BEGIN
  PERFORM fn_exigir_tenant(p_idEmp);
  SELECT d.monto INTO v_tope
  FROM devolucion d
  JOIN pago p ON p.idEmp=d.idEmp AND p.nroVenta=d.nroVenta
  WHERE d.idEmp=p_idEmp AND d.nroDev=p_nroDev
    AND d.estado='aprobada' AND p.idPago=p_idPago;

  IF v_tope IS NULL OR p_monto IS NULL OR p_monto < 0 OR p_monto > v_tope THEN
    RAISE EXCEPTION 'Reembolso no permitido: revise empresa, venta, pago y monto autorizado';
  END IF;

  IF EXISTS (SELECT 1 FROM reembolso WHERE idEmp=p_idEmp AND nroDev=p_nroDev
             AND estado IN ('pendiente', 'aprobado')) THEN
    RAISE EXCEPTION 'La devolución ya tiene un reembolso pendiente o aprobado';
  END IF;

  INSERT INTO reembolso (idEmp, metodo, monto, estado, idPago, nroDev)
  VALUES (p_idEmp, p_metodo, p_monto, 'aprobado', p_idPago, p_nroDev);
END;
$$;

-- Impide dos reembolsos aprobados/pendientes para la misma devolución,
-- incluso si llegan simultáneamente desde dos solicitudes.
-- Índice uq_reembolso_emp_dev_vigente ya definido en archivo 01.

-- ---------- 4.10 Gestionar Inventario: registrar movimiento manual ----------
CREATE OR REPLACE PROCEDURE sp_registrar_movimiento_inventario(
  p_idEmp INTEGER, p_nroInv INTEGER, p_tipoMov VARCHAR, p_cantidad INTEGER, p_motivo VARCHAR
)
LANGUAGE plpgsql
AS $$
BEGIN
  PERFORM fn_exigir_tenant(p_idEmp);
  IF p_cantidad IS NULL OR p_cantidad <= 0 THEN
    RAISE EXCEPTION 'La cantidad debe ser mayor que cero';
  END IF;

  IF p_tipoMov IN ('entrada','ajuste') THEN
    UPDATE inventario SET stock = stock + p_cantidad, cantDisp = cantDisp + p_cantidad
    WHERE nroInv = p_nroInv AND idEmp = p_idEmp;
  ELSIF p_tipoMov = 'salida' THEN
    UPDATE inventario SET stock = stock - p_cantidad, cantDisp = cantDisp - p_cantidad
    WHERE nroInv = p_nroInv AND idEmp = p_idEmp;
  ELSE
    RAISE EXCEPTION 'Tipo de movimiento inválido: %', p_tipoMov;
  END IF;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'El inventario % no existe en la empresa %', p_nroInv, p_idEmp;
  END IF;

  INSERT INTO movimiento_inv (idEmp, tipoMov, cantidad, fecha, motivo, nroInv)
  VALUES (p_idEmp, p_tipoMov, p_cantidad, CURRENT_DATE, p_motivo, p_nroInv);
END;
$$;

-- ==========================================================
-- 5) POLITICAS POR EMPRESA (CU23)
-- ==========================================================
-- Solo el administrador autenticado del TENANT puede llamar por la API.
-- El backend comprueba rol; PostgreSQL comprueba pertenencia por RLS/FK.
CREATE OR REPLACE PROCEDURE sp_configurar_politica_general(
  p_idEmp integer, p_dias integer, p_porcentaje numeric, p_condiciones text
) LANGUAGE plpgsql AS $$
BEGIN
  PERFORM fn_exigir_tenant(p_idEmp);
  IF p_dias IS NULL OR p_dias < 0
     OR p_porcentaje IS NULL OR p_porcentaje <= 0 OR p_porcentaje > 100 THEN
    RAISE EXCEPTION 'Politica general fuera de los rangos permitidos';
  END IF;
  UPDATE empresa SET diasPlazoDevolucion=p_dias,
    porcentajeDevolucion=p_porcentaje, condicionesDevolucion=p_condiciones
  WHERE idEmp=p_idEmp;
  IF NOT FOUND THEN RAISE EXCEPTION 'Empresa no encontrada'; END IF;
END;
$$;

CREATE OR REPLACE PROCEDURE sp_configurar_politica_producto(
  p_idEmp integer, p_idProd varchar, p_idVar varchar,
  p_dias integer, p_porcentaje numeric
) LANGUAGE plpgsql AS $$
BEGIN
  PERFORM fn_exigir_tenant(p_idEmp);
  IF p_dias IS NULL OR p_dias <= 0 OR p_porcentaje IS NULL
     OR p_porcentaje <= 0 OR p_porcentaje > 100 THEN
    RAISE EXCEPTION 'Plazo o porcentaje invalido';
  END IF;
  -- FK (idEmp,idProd,idVar) verifica que la variante sea del producto.
  INSERT INTO politica_devolucion (idEmp,idprod,idvar,dias,porcentaje)
  VALUES (p_idEmp,p_idProd,p_idVar,p_dias,p_porcentaje)
  ON CONFLICT (idEmp,idprod)
  DO UPDATE SET idvar=EXCLUDED.idvar,dias=EXCLUDED.dias,
                porcentaje=EXCLUDED.porcentaje;
END;
$$;

-- ==========================================================
-- 6) VENTA DE CONTADO ATOMICA / OFFLINE (CU24)
-- ==========================================================
-- Centraliza validacion de stock, precio, venta, detalles, pago y movimientos.
-- Las ventas ONLINE existentes del backend deben elegir UN SOLO mecanismo
-- de descuento de stock (este PA o transaccion backend), NUNCA ambos.
-- El UUID de origen se registra en venta y evita duplicaciones por reintento.
CREATE OR REPLACE PROCEDURE sp_registrar_venta_contado(
  p_idEmp integer, p_nroSuc integer, p_idUsuarioEmp varchar,
  p_idUsuarioCl varchar, p_idVars varchar[], p_cantidades integer[],
  p_precios numeric[], p_idOperacion uuid, p_origen varchar,
  INOUT p_nroVenta integer DEFAULT NULL
) LANGUAGE plpgsql AS $$
DECLARE
  i integer;
  v_stock integer;
  v_precio numeric(10,2);
  v_total numeric(10,2):=0;
  v_nroInv integer;
BEGIN
  PERFORM fn_exigir_tenant(p_idEmp);
  IF p_origen <> 'servidor' AND NOT fn_tiene_funcionalidad(p_idEmp,'OFFLINE') THEN
    RAISE EXCEPTION 'La suscripcion no habilita el registro offline';
  END IF;
  IF p_idOperacion IS NOT NULL THEN
    PERFORM pg_advisory_xact_lock(p_idEmp,hashtext(p_idOperacion::text));
    SELECT nroVenta INTO p_nroVenta FROM venta
      WHERE idEmp=p_idEmp AND idOperacion=p_idOperacion;
    IF FOUND THEN RETURN; END IF;
  END IF;
  IF p_origen NOT IN ('servidor','pwa','movil') OR p_origen IS NULL THEN
    RAISE EXCEPTION 'Origen de operacion invalido';
  END IF;
  IF p_origen <> 'servidor' AND p_idOperacion IS NULL THEN
    RAISE EXCEPTION 'Una venta offline requiere un UUID';
  END IF;
  IF cardinality(p_idVars) = 0 OR p_idVars IS NULL
     OR cardinality(p_idVars) <> cardinality(p_cantidades)
     OR cardinality(p_idVars) <> cardinality(p_precios) THEN
    RAISE EXCEPTION 'Listas de variantes, cantidades y precios invalidas';
  END IF;
  IF (SELECT count(DISTINCT x) FROM unnest(p_idVars) AS a(x)) <> cardinality(p_idVars) THEN
    RAISE EXCEPTION 'No repita variantes en una misma venta';
  END IF;
  PERFORM 1 FROM empleado
    WHERE idEmp=p_idEmp AND idUsuario=p_idUsuarioEmp AND nroSuc=p_nroSuc FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'El cajero no pertenece a esta sucursal'; END IF;
  PERFORM 1 FROM sucursal WHERE idEmp=p_idEmp AND nro=p_nroSuc AND estado='activo';
  IF NOT FOUND THEN RAISE EXCEPTION 'Sucursal inexistente o inactiva'; END IF;

  -- El orden de las filas bloqueadas es estable para disminuir deadlocks.
  PERFORM 1 FROM inventario
    WHERE idEmp=p_idEmp AND nroSuc=p_nroSuc AND idVar=ANY(p_idVars)
    ORDER BY idVar FOR UPDATE;
  FOR i IN 1..cardinality(p_idVars) LOOP
    IF p_idVars[i] IS NULL OR p_cantidades[i] IS NULL OR p_cantidades[i] <= 0
       OR p_precios[i] IS NULL OR p_precios[i] <= 0 THEN
      RAISE EXCEPTION 'Datos invalidos en la linea %',i;
    END IF;
    SELECT inv.nroInv,inv.cantDisp,vp.precio
      INTO v_nroInv,v_stock,v_precio
    FROM inventario inv
    JOIN varianteProd vp ON vp.idEmp=inv.idEmp AND vp.idVariante=inv.idVar
    WHERE inv.idEmp=p_idEmp AND inv.nroSuc=p_nroSuc AND inv.idVar=p_idVars[i];
    IF NOT FOUND OR v_stock < p_cantidades[i] THEN
      RAISE EXCEPTION 'STOCK_INSUFICIENTE para variante %',p_idVars[i];
    END IF;
    IF round(p_precios[i],2) <> v_precio THEN
      RAISE EXCEPTION 'PRECIO_CAMBIADO para variante %',p_idVars[i];
    END IF;
    v_total:=v_total + round(p_cantidades[i] * p_precios[i],2);
  END LOOP;

  INSERT INTO venta(idEmp,fechaHora,total,desc_aplicado,estado,
                    idUsuarioCl,idUsuarioEmp,nroSuc,idOperacion,origen,fechaSync)
  VALUES (p_idEmp,localtimestamp,v_total,0,'registrada',p_idUsuarioCl,p_idUsuarioEmp,
          p_nroSuc,p_idOperacion,p_origen,
          CASE WHEN p_origen='servidor' THEN NULL ELSE now() END)
  RETURNING nroVenta INTO p_nroVenta;

  FOR i IN 1..cardinality(p_idVars) LOOP
    INSERT INTO detalleVenta(idEmp,nroVenta,idDetalleVenta,cantidad,precioUnitario,idVar)
    VALUES (p_idEmp,p_nroVenta,i,p_cantidades[i],p_precios[i],p_idVars[i]);
    UPDATE inventario
      SET stock=stock-p_cantidades[i],cantDisp=cantDisp-p_cantidades[i]
      WHERE idEmp=p_idEmp AND nroSuc=p_nroSuc AND idVar=p_idVars[i];
    INSERT INTO movimiento_inv(idEmp,tipoMov,cantidad,fecha,motivo,nroInv)
    SELECT p_idEmp,'salida',p_cantidades[i],current_date,
           'Venta contado #'||p_nroVenta,inv.nroInv
    FROM inventario inv WHERE inv.idEmp=p_idEmp AND inv.nroSuc=p_nroSuc AND inv.idVar=p_idVars[i];
  END LOOP;
  INSERT INTO pago(idEmp,metodo,monto,estado,nroVenta)
  VALUES(p_idEmp,'efectivo',v_total,'aprobado',p_nroVenta);
END;
$$;

-- Gestionar cambio (CU23). Registrar un cambio primero en pendiente;
-- luego aprobación con sp_aprobar_cambio hace ajuste via trigger.
CREATE OR REPLACE PROCEDURE sp_registrar_cambio(
  p_idEmp integer,p_nroVenta integer,p_nroSuc integer,p_idUsuarioEmp varchar,
  p_lineasVenta integer[],p_variantesNuevas varchar[],p_cantidades integer[],
  p_preciosNuevos numeric[],p_motivo varchar,
  INOUT p_nroCambio integer DEFAULT NULL
) LANGUAGE plpgsql AS $$
DECLARE
  i integer;
BEGIN
  PERFORM fn_exigir_tenant(p_idEmp);
  IF cardinality(p_lineasVenta)=0 OR p_lineasVenta IS NULL
    OR cardinality(p_lineasVenta)<>cardinality(p_variantesNuevas)
    OR cardinality(p_lineasVenta)<>cardinality(p_cantidades)
    OR cardinality(p_lineasVenta)<>cardinality(p_preciosNuevos) THEN
    RAISE EXCEPTION 'Detalles de cambio invalidos';
  END IF;
  IF (SELECT count(DISTINCT x) FROM unnest(p_lineasVenta) AS a(x))
      <> cardinality(p_lineasVenta) THEN
    RAISE EXCEPTION 'No repetir una linea de venta en el mismo cambio';
  END IF;
  PERFORM 1 FROM venta WHERE idEmp=p_idEmp AND nroVenta=p_nroVenta
    AND estado='registrada' FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'Venta inexistente o anulada'; END IF;
  PERFORM 1 FROM empleado WHERE idEmp=p_idEmp AND idUsuario=p_idUsuarioEmp
    AND nroSuc=p_nroSuc;
  IF NOT FOUND THEN RAISE EXCEPTION 'Operador no autorizado para esta sucursal'; END IF;
  INSERT INTO cambio(idEmp,nroVenta,nroSuc,idUsuarioEmp,estado,motivo)
  VALUES (p_idEmp,p_nroVenta,p_nroSuc,p_idUsuarioEmp,'pendiente',p_motivo)
  RETURNING nroCambio INTO p_nroCambio;
  FOR i IN 1..cardinality(p_lineasVenta) LOOP
    IF p_cantidades[i] IS NULL OR p_cantidades[i]<=0
       OR p_preciosNuevos[i] IS NULL OR p_preciosNuevos[i]<=0 THEN
      RAISE EXCEPTION 'Cantidad o precio invalido en linea %', i;
    END IF;
    INSERT INTO detalleCambio(idEmp,nroCambio,idDetalleCambio,cantidad,nroVenta,
                             idDetalleVenta,idVarNueva,precioNuevo)
    VALUES (p_idEmp,p_nroCambio,i,p_cantidades[i],p_nroVenta,
            p_lineasVenta[i],p_variantesNuevas[i],p_preciosNuevos[i]);
  END LOOP;
END;
$$;
CREATE OR REPLACE PROCEDURE sp_aprobar_cambio(p_idEmp integer,p_nroCambio integer)
LANGUAGE plpgsql AS $$
BEGIN
  PERFORM fn_exigir_tenant(p_idEmp);
  UPDATE cambio SET estado='aprobado'
  WHERE idEmp=p_idEmp AND nroCambio=p_nroCambio AND estado='pendiente';
  IF NOT FOUND THEN RAISE EXCEPTION 'Cambio inexistente o ya procesado'; END IF;
END;
$$;

-- ==========================================================
-- 7) RESULTADOS DE SINCRONIZACION / CONFLICTOS (CU11, CU24, CU36)
-- ==========================================================
-- Para un fallo comercial dentro de una transaccion: la API hace ROLLBACK,
-- despues registra el rechazo/conflicto en OTRA transaccion. No se borra
-- la operacion del dispositivo: permanece para conciliacion.
CREATE OR REPLACE PROCEDURE sp_registrar_resultado_sync(
  p_idEmp integer, p_uuid uuid, p_tipo varchar, p_nroSuc integer,
  p_idUsuario varchar, p_fechaLocal timestamptz, p_payload jsonb,
  p_estado varchar, p_codigoMotivo varchar DEFAULT NULL,
  p_detalle text DEFAULT NULL, p_nroVenta integer DEFAULT NULL,
  p_nroReserva integer DEFAULT NULL
) LANGUAGE plpgsql AS $$
BEGIN
  PERFORM fn_exigir_tenant(p_idEmp);
  IF p_estado NOT IN ('recibida','confirmada','rechazada','conflicto')
     OR p_uuid IS NULL OR p_payload IS NULL THEN
    RAISE EXCEPTION 'Datos invalidos de sincronizacion';
  END IF;
  IF p_estado='confirmada' AND
     ((p_tipo='venta' AND p_nroVenta IS NULL)
       OR (p_tipo='reserva' AND p_nroReserva IS NULL)) THEN
    RAISE EXCEPTION 'La confirmacion exige un documento aceptado por el servidor';
  END IF;
  IF p_estado='confirmada' AND p_tipo='venta' AND NOT EXISTS (
      SELECT 1 FROM venta WHERE idEmp=p_idEmp AND nroVenta=p_nroVenta
        AND idOperacion=p_uuid) THEN
    RAISE EXCEPTION 'La venta confirmada no corresponde al UUID recibido';
  END IF;
  IF p_estado='confirmada' AND p_tipo='reserva' AND NOT EXISTS (
      SELECT 1 FROM reserva WHERE idEmp=p_idEmp AND nroReserva=p_nroReserva
        AND idOperacion=p_uuid) THEN
    RAISE EXCEPTION 'La reserva confirmada no corresponde al UUID recibido';
  END IF;
  INSERT INTO operacion_sync(idEmp,idOperacion,tipo,nroSuc,idUsuario,fechaLocal,
     payload,estado,codigoMotivo,detalleMotivo,nroVenta,nroReserva)
  VALUES (p_idEmp,p_uuid,p_tipo,p_nroSuc,p_idUsuario,p_fechaLocal,
     p_payload,p_estado,p_codigoMotivo,p_detalle,p_nroVenta,p_nroReserva)
  ON CONFLICT (idEmp,idOperacion) DO UPDATE
    SET intentos=operacion_sync.intentos+1,
        estado=CASE WHEN operacion_sync.estado='confirmada'
                   THEN 'confirmada' ELSE EXCLUDED.estado END,
        codigoMotivo=CASE WHEN operacion_sync.estado='confirmada'
                   THEN operacion_sync.codigoMotivo ELSE EXCLUDED.codigoMotivo END,
        detalleMotivo=CASE WHEN operacion_sync.estado='confirmada'
                   THEN operacion_sync.detalleMotivo ELSE EXCLUDED.detalleMotivo END,
        nroVenta=COALESCE(operacion_sync.nroVenta,EXCLUDED.nroVenta),
        nroReserva=COALESCE(operacion_sync.nroReserva,EXCLUDED.nroReserva);
END;
$$;

CREATE OR REPLACE PROCEDURE sp_resolver_conflicto_sync(
  p_idEmp integer, p_uuid uuid, p_idResponsable varchar,
  p_estado varchar, p_detalle text
) LANGUAGE plpgsql AS $$
BEGIN
  PERFORM fn_exigir_tenant(p_idEmp);
  IF p_estado NOT IN ('rechazada','confirmada') THEN
    RAISE EXCEPTION 'Estado de conciliacion no soportado';
  END IF;
  IF p_estado='confirmada' AND NOT EXISTS (
    SELECT 1 FROM operacion_sync os WHERE os.idEmp=p_idEmp
      AND os.idOperacion=p_uuid AND (os.nroVenta IS NOT NULL OR os.nroReserva IS NOT NULL)
  ) THEN
    RAISE EXCEPTION 'No confirmar sin la venta o reserva efectivamente registrada';
  END IF;
  UPDATE operacion_sync
  SET estado=p_estado,resueltaPor=p_idResponsable,fechaResolucion=now(),
      detalleMotivo=p_detalle
  WHERE idEmp=p_idEmp AND idOperacion=p_uuid AND estado='conflicto';
  IF NOT FOUND THEN RAISE EXCEPTION 'Conflicto inexistente o ya resuelto'; END IF;
END;
$$;

-- Los procedimientos de administración general solo los ejecuta
-- un rol DB de aprovisionamiento dedicado (GRANT explícito fuera de estos archivos).
-- PostgreSQL concede EXECUTE sobre nuevas rutinas a PUBLIC por defecto;
-- por ello se revoca para estas operaciones privilegiadas.
DO $$
DECLARE x record;
BEGIN
  FOR x IN SELECT oid::regprocedure AS sign
    FROM pg_proc
    WHERE pronamespace='public'::regnamespace
      AND proname IN ('sp_registrar_empresa','sp_cambiar_plan',
         'sp_actualizar_suscripciones_vencidas','sp_marcar_reservas_vencidas')
      AND prokind='p'
  LOOP
    EXECUTE format('REVOKE ALL ON PROCEDURE %s FROM PUBLIC', x.sign);
  END LOOP;
END $$;

-- ==========================================================
-- 8) ROW LEVEL SECURITY (PostgreSQL, aislamiento entre empresas)
-- ==========================================================
-- Las FK compuestas evitan asociaciones cruzadas. RLS evita lecturas cruzadas.
-- SOLO el backend autenticado configura SET LOCAL app.idemp con el tenant validado.
-- RLS NO sustituye los permisos comerciales por rol (CU05/CU06).
-- NO utilizar postgres/superuser ni un rol con BYPASSRLS para solicitudes normales.
-- Provision de empresa SaaS y cron global: rol de servicio SEPARADO.
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'empresa','suscripcion','rol','rol_funcion','usuario','bitacora','recuperacion_contrasena',
    'admin','ciudad','sucursal','empleado','cliente','horario_trab',
    'horario_emp','horario_atencion','horario_suc','talla','color',
    'promocion','categoria','marca','coleccion','temporada','tempColeccion',
    'proveedor','producto','varianteProd','varianteColors',
    'politica_devolucion','inventario','movimiento_inv','carrito','detalleCarro',
    'reserva','detalleReserva','venta','detalleVenta','devolucion','detalleDev',
    'pago','reembolso','cambio','detalleCambio','operacion_sync','reporte_solicitud'
  ] LOOP
    EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY',lower(t));
    EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY',lower(t));
    EXECUTE format('CREATE POLICY tenant_isolation ON public.%I USING (idEmp = fn_idemp_actual()) WITH CHECK (idEmp = fn_idemp_actual())',lower(t));
  END LOOP;
END $$;

-- IMPORTANTE: plan, funcionalidad_saas, plan_funcionalidad,
-- funcion y reporte_tipo son globales. El BACKEND debe autorizar sus operaciones.
-- Un rol DB de plataforma (no el rol web común) administra provisioning.

COMMIT;
