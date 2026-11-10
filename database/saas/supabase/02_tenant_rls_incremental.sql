-- Fase incremental tenant-RLS, adaptada del bucle canónico de SQL 02.
-- No crea ni cambia roles, funciones, triggers ni datos.
-- Ejecutar en staging con psql -v target_db=<nombre exacto>.
-- Requiere que 07_access_grants_incremental.sql (PARTE A: lectura) se haya
-- ejecutado antes, para que fashionstore_app no se quede sin acceso al activar RLS.
-- Requiere que fashionstore_app exista y sea NOBYPASSRLS.
\set ON_ERROR_STOP on
\if :{?target_db}
\else
  \echo 'Falta -v target_db=<base esperada>; no se aplicó la migración.'
  \quit 3
\endif

BEGIN;
SELECT pg_catalog.set_config('fashionstore.expected_migration_db', :'target_db', true);

DO $$
DECLARE
  tenant_tables text[] := ARRAY[
    'empresa','suscripcion','rol','rol_funcion','usuario','bitacora',
    'recuperacion_contrasena','admin','ciudad','sucursal','empleado','cliente',
    'horario_trab','horario_emp','horario_atencion','horario_suc','talla','color',
    'promocion','categoria','marca','coleccion','temporada','tempcoleccion',
    'proveedor','producto','varianteprod','variantecolors','politica_devolucion',
    'inventario','movimiento_inv','carrito','detallecarro','reserva','detallereserva',
    'venta','detalleventa','devolucion','detalledev','pago','reembolso','cambio',
    'detallecambio','operacion_sync','reporte_solicitud'
  ];
  t text;
  table_oid oid;
  v_select_priv boolean;
BEGIN
  IF current_database() <> current_setting('fashionstore.expected_migration_db') THEN
    RAISE EXCEPTION 'Base de destino distinta de target_db; rollback requerido';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles
                 WHERE rolname='fashionstore_app' AND NOT rolsuper AND NOT rolbypassrls) THEN
    RAISE EXCEPTION 'fashionstore_app debe existir y ser NOBYPASSRLS';
  END IF;
  IF to_regprocedure('public.fn_idemp_actual()') IS NULL THEN
    RAISE EXCEPTION 'Falta fn_idemp_actual(); ejecutar 05_tenant_auth_incremental.sql primero';
  END IF;
  -- Validar que fashionstore_app tiene SELECT en las tablas tenant antes de activar RLS.
  -- Sin esto, activar RLS dejaría a la API HTTP sin acceso a los datos.
  FOREACH t IN ARRAY tenant_tables LOOP
    table_oid := to_regclass(format('public.%I', t));
    IF table_oid IS NULL THEN
      RAISE EXCEPTION 'Falta tabla tenant public.%', t;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_attribute
                   WHERE attrelid=table_oid AND attname='idemp' AND NOT attisdropped) THEN
      RAISE EXCEPTION 'public.% no tiene columna idemp; requiere decisión de mapeo', t;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_policies
               WHERE schemaname='public' AND tablename=t AND policyname='tenant_isolation') THEN
      RAISE EXCEPTION 'Ya existe policy tenant_isolation en public.%', t;
    END IF;
    SELECT has_table_privilege('fashionstore_app', table_oid, 'SELECT') INTO v_select_priv;
    IF NOT v_select_priv THEN
      RAISE EXCEPTION 'fashionstore_app no tiene SELECT en public.%; ejecutar 07_access_grants_incremental.sql PARTE A antes de activar RLS', t;
    END IF;
  END LOOP;
END $$;

DO $$
DECLARE
  t text;
  tenant_tables text[] := ARRAY[
    'empresa','suscripcion','rol','rol_funcion','usuario','bitacora',
    'recuperacion_contrasena','admin','ciudad','sucursal','empleado','cliente',
    'horario_trab','horario_emp','horario_atencion','horario_suc','talla','color',
    'promocion','categoria','marca','coleccion','temporada','tempcoleccion',
    'proveedor','producto','varianteprod','variantecolors','politica_devolucion',
    'inventario','movimiento_inv','carrito','detallecarro','reserva','detallereserva',
    'venta','detalleventa','devolucion','detalledev','pago','reembolso','cambio',
    'detallecambio','operacion_sync','reporte_solicitud'
  ];
BEGIN
  FOREACH t IN ARRAY tenant_tables LOOP
    EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format(
      'CREATE POLICY tenant_isolation ON public.%I USING (idemp = public.fn_idemp_actual()) WITH CHECK (idemp = public.fn_idemp_actual())',
      t);
  END LOOP;
END $$;

COMMIT;
