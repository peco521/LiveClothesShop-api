-- Grants explícitos para FashionStore. Pendiente de autorización para Supabase.
-- No altera atributos, miembros ni contraseñas de roles.
-- Las revocaciones de PUBLIC/anon/authenticated se hacen en una fase posterior,
-- tras pruebas HTTP y revisión de consumidores externos.
--
-- EJECUCIÓN COORDINADA CON 02_tenant_rls_incremental.sql:
--   1. Ejecutar este archivo hasta el marcador "FIN PARTE A" (grants de lectura).
--   2. Ejecutar 02_tenant_rls_incremental.sql (activa RLS + políticas tenant_isolation).
--   3. Ejecutar el resto de este archivo desde "INICIO PARTE B" (grants de escritura
--      y EXECUTE). Con RLS activo, fashionstore_app solo puede modificar filas de su
--      propio tenant porque es NOBYPASSRLS.
\set ON_ERROR_STOP on
\if :{?target_db}
\else
  \echo 'Falta -v target_db=<base esperada>; no se aplicó la migración.'
  \quit 3
\endif

BEGIN;
SELECT set_config('fashionstore.expected_migration_db', :'target_db', true);
DO $$
BEGIN
  IF current_database() <> current_setting('fashionstore.expected_migration_db') THEN
    RAISE EXCEPTION 'Destino inesperado';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='fashionstore_app' AND NOT rolsuper AND NOT rolbypassrls) THEN
    RAISE EXCEPTION 'fashionstore_app debe existir, sin SUPERUSER ni BYPASSRLS';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='fashionstore_provisioner' AND rolbypassrls) THEN
    RAISE EXCEPTION 'fashionstore_provisioner debe existir con BYPASSRLS';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='fashionstore_backup' AND rolbypassrls) THEN
    RAISE EXCEPTION 'fashionstore_backup debe existir con BYPASSRLS para respaldar filas RLS';
  END IF;
END $$;

GRANT USAGE ON SCHEMA public TO fashionstore_app, fashionstore_provisioner, fashionstore_backup;
GRANT USAGE, CREATE ON SCHEMA public TO fashionstore_owner;

-- ============================================================
-- PARTE A: Grants de lectura a fashionstore_app.
-- Ejecutar ANTES de 02_tenant_rls_incremental.sql.
-- Sin estos SELECT, activar RLS dejaría a la API HTTP sin acceso.
-- ============================================================
GRANT SELECT ON public.empresa, public.suscripcion, public.plan, public.funcionalidad_saas,
  public.plan_funcionalidad, public.plan_version, public.plan_version_funcionalidad,
  public.suscripcion_plan_version, public.funcion, public.reporte_tipo
TO fashionstore_app;
-- FIN PARTE A
-- ============================================================

-- ============================================================
-- INICIO PARTE B: Grants de escritura y EXECUTE a fashionstore_app.
-- Ejecutar DESPUÉS de 02_tenant_rls_incremental.sql.
-- Con RLS activo y fashionstore_app NOBYPASSRLS, estos grants solo afectan
-- filas del tenant establecido en app.idemp para la sesión.
-- ============================================================
GRANT SELECT, INSERT, UPDATE, DELETE ON
  public.rol, public.rol_funcion, public.usuario, public.bitacora,
  public.recuperacion_contrasena, public.admin, public.ciudad, public.sucursal,
  public.empleado, public.cliente, public.horario_trab, public.horario_emp,
  public.horario_atencion, public.horario_suc, public.talla, public.color,
  public.promocion, public.categoria, public.marca, public.coleccion,
  public.temporada, public.tempcoleccion, public.proveedor, public.producto,
  public.varianteprod, public.variantecolors, public.politica_devolucion,
  public.inventario, public.movimiento_inv, public.carrito, public.detallecarro,
  public.reserva, public.detallereserva, public.venta, public.detalleventa,
  public.devolucion, public.detalledev, public.pago, public.reembolso,
  public.cambio, public.detallecambio, public.operacion_sync, public.reporte_solicitud
TO fashionstore_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO fashionstore_app;
GRANT EXECUTE ON FUNCTION public.fn_idemp_actual(), public.fn_exigir_tenant(integer),
  public.fn_suscripcion_vigente(integer), public.fn_tiene_funcionalidad(integer,varchar),
  public.fn_max_sucursales(integer), public.auth_resolver_empresa(varchar)
TO fashionstore_app;
-- Se concede por 08_password_recovery_resolver_incremental.sql cuando la
-- rutina SECURITY DEFINER de recuperación ya exista y haya sido validada.
-- Solo rutinas llamadas por operaciones empresariales de FastAPI. Las de
-- plataforma y mantenimiento transversal no se conceden al rol HTTP tenant.
GRANT EXECUTE ON PROCEDURE
  public.sp_registrar_cliente(integer,varchar,varchar,varchar,varchar,varchar,character,varchar,varchar,varchar,varchar,date,varchar,varchar),
  public.sp_cambiar_contrasena(integer,varchar,varchar),
  public.sp_agregar_al_carrito(integer,varchar,varchar,integer),
  public.sp_realizar_reserva(integer,varchar,integer,date,time,varchar[],integer[],integer,uuid,varchar),
  public.sp_cancelar_reserva(integer,integer),
  public.sp_registrar_devolucion(integer,integer,numeric,integer[],integer[],integer),
  public.sp_aprobar_devolucion(integer,integer),
  public.sp_registrar_movimiento_inventario(integer,integer,varchar,integer,varchar),
  public.sp_procesar_reembolso(integer,integer,integer,numeric,varchar)
TO fashionstore_app;
-- Rutinas de sincronización offline y aprobación de cambios (09_sync_offline
-- y 12_devoluciones_cambios). Concedidas a fashionstore_app tras validación en
-- el entorno aislado. fashionstore_provisioner ya recibe EXECUTE sobre todas
-- las rutinas por la cláusula ALL FUNCTIONS/PROCEDURES más abajo.
GRANT EXECUTE ON FUNCTION public.fn_sync_tomar_operacion(integer,uuid,varchar,integer,varchar,timestamptz,jsonb,char(64),varchar,smallint), public.fn_sync_estado_operacion(integer,uuid) TO fashionstore_app;
GRANT EXECUTE ON PROCEDURE public.sp_sync_confirmar(integer,uuid,integer,integer), public.sp_sync_rechazar(integer,uuid,varchar,varchar,text), public.sp_sync_registrar_rechazo(integer,uuid,varchar,integer,varchar,timestamptz,jsonb,char(64),varchar,smallint,varchar,varchar,text), public.sp_registrar_venta_offline(integer,integer,varchar,varchar,varchar[],integer[],numeric[],uuid,varchar,varchar,numeric,varchar,integer), public.sp_resolver_conflicto_sync(integer,uuid,varchar,varchar,text), public.sp_aprobar_cambio(integer,integer) TO fashionstore_app;
-- FIN PARTE B
-- ============================================================

-- Plataforma: solo la API SaaS usa esta identidad, nunca Angular/Flutter.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO fashionstore_provisioner;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO fashionstore_provisioner;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO fashionstore_provisioner;
GRANT EXECUTE ON ALL PROCEDURES IN SCHEMA public TO fashionstore_provisioner;

-- Respaldo sin DML ni EXECUTE.
GRANT SELECT ON ALL TABLES IN SCHEMA public TO fashionstore_backup;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO fashionstore_backup;

COMMIT;
