-- FashionStore - Roles de base de datos y permisos minimos.
-- Ejecutar como superusuario del PostgreSQL DE PRUEBAS, despues de 01, 02 y 03.
--
-- Contrato:
--   fashionstore_owner        propietario del esquema (DDL). NO se usa para HTTP.
--   fashionstore_provisioner  aprovisionamiento y panel SaaS. BYPASSRLS explicito.
--   fashionstore_app          peticiones HTTP de empresa. NOBYPASSRLS, nunca owner.
--
-- Las contrasenas NO se escriben en este archivo: se reciben como variables de
-- psql generadas localmente por 00_preparar_postgres_pruebas.ps1.
\set ON_ERROR_STOP on

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='fashionstore_owner') THEN
    CREATE ROLE fashionstore_owner NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='fashionstore_app') THEN
    CREATE ROLE fashionstore_app LOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='fashionstore_provisioner') THEN
    CREATE ROLE fashionstore_provisioner LOGIN;
  END IF;
END $$;

ALTER ROLE fashionstore_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
ALTER ROLE fashionstore_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOINHERIT;
ALTER ROLE fashionstore_provisioner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS NOINHERIT;

ALTER ROLE fashionstore_app PASSWORD :'app_password';
ALTER ROLE fashionstore_provisioner PASSWORD :'provisioner_password';

REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO fashionstore_app, fashionstore_provisioner;

-- ------------------------------------------------------------------
-- Rol HTTP de empresa: DML solo sobre tablas con idEmp.
-- ------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE, DELETE ON
  empresa, suscripcion, rol, rol_funcion, usuario, bitacora, recuperacion_contrasena,
  admin, ciudad, sucursal, empleado, cliente, horario_trab, horario_emp,
  horario_atencion, horario_suc, talla, color, promocion, categoria, marca,
  coleccion, temporada, tempcoleccion, proveedor, producto, varianteprod,
  variantecolors, politica_devolucion, inventario, movimiento_inv, carrito,
  detallecarro, reserva, detallereserva, venta, detalleventa, devolucion,
  detalledev, pago, reembolso, cambio, detallecambio, operacion_sync,
  reporte_solicitud
TO fashionstore_app;

-- Catalogos globales: solo lectura.
GRANT SELECT ON plan, funcionalidad_saas, plan_funcionalidad,
  plan_version, plan_version_funcionalidad, suscripcion_plan_version,
  funcion, reporte_tipo
TO fashionstore_app;

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO fashionstore_app;

-- Rutinas de negocio autorizadas al rol HTTP.
GRANT EXECUTE ON FUNCTION auth_resolver_empresa(varchar) TO fashionstore_app;
GRANT EXECUTE ON FUNCTION fn_idemp_actual() TO fashionstore_app;
GRANT EXECUTE ON FUNCTION fn_exigir_tenant(integer) TO fashionstore_app;
GRANT EXECUTE ON FUNCTION fn_suscripcion_vigente(integer) TO fashionstore_app;
GRANT EXECUTE ON FUNCTION fn_tiene_funcionalidad(integer, varchar) TO fashionstore_app;
GRANT EXECUTE ON FUNCTION fn_max_sucursales(integer) TO fashionstore_app;
GRANT EXECUTE ON FUNCTION fn_sync_tomar_operacion(integer, uuid, varchar, integer, varchar, timestamptz, jsonb, char, varchar, smallint) TO fashionstore_app;
GRANT EXECUTE ON FUNCTION fn_sync_estado_operacion(integer, uuid) TO fashionstore_app;

GRANT EXECUTE ON PROCEDURE
  sp_registrar_cliente(integer, varchar, varchar, varchar, varchar, varchar, char, varchar, varchar, varchar, varchar, date, varchar, varchar),
  sp_cambiar_contrasena(integer, varchar, varchar),
  sp_agregar_al_carrito(integer, varchar, varchar, integer),
  sp_realizar_reserva(integer, varchar, integer, date, time, varchar[], integer[], integer, uuid, varchar),
  sp_cancelar_reserva(integer, integer),
  sp_registrar_devolucion(integer, integer, numeric, integer[], integer[], integer),
  sp_aprobar_devolucion(integer, integer),
  sp_procesar_reembolso(integer, integer, integer, numeric, varchar),
  sp_registrar_movimiento_inventario(integer, integer, varchar, integer, varchar),
  sp_configurar_politica_general(integer, integer, numeric, text),
  sp_configurar_politica_producto(integer, varchar, varchar, integer, numeric),
  sp_registrar_cambio(integer, integer, integer, varchar, integer[], varchar[], integer[], numeric[], varchar, integer),
  sp_aprobar_cambio(integer, integer),
  sp_registrar_venta_contado(integer, integer, varchar, varchar, varchar[], integer[], numeric[], uuid, varchar, integer),
  sp_registrar_venta_offline(integer, integer, varchar, varchar, varchar[], integer[], numeric[], uuid, varchar, varchar, numeric, varchar, integer),
  sp_registrar_resultado_sync(integer, uuid, varchar, integer, varchar, timestamptz, jsonb, varchar, varchar, text, integer, integer),
  sp_resolver_conflicto_sync(integer, uuid, varchar, varchar, text),
  sp_sync_confirmar(integer, uuid, integer, integer),
  sp_sync_rechazar(integer, uuid, varchar, varchar, text),
  sp_sync_registrar_rechazo(integer, uuid, varchar, integer, varchar, timestamptz, jsonb, char, varchar, smallint, varchar, varchar, text)
TO fashionstore_app;

-- El rol HTTP nunca administra plataforma ni tareas globales.
REVOKE ALL ON PROCEDURE sp_registrar_empresa(varchar, varchar, varchar, smallint, date, date, varchar, varchar, varchar, varchar, varchar, char, varchar, varchar, varchar, varchar, date, integer) FROM fashionstore_app;
REVOKE ALL ON PROCEDURE sp_cambiar_plan(integer, smallint, date, date) FROM fashionstore_app;
REVOKE ALL ON PROCEDURE sp_actualizar_suscripciones_vencidas() FROM fashionstore_app;
REVOKE ALL ON PROCEDURE sp_marcar_reservas_vencidas(integer) FROM fashionstore_app;
REVOKE ALL ON PROCEDURE sp_plataforma_estado_empresa(integer, varchar) FROM fashionstore_app;
REVOKE ALL ON PROCEDURE sp_plataforma_identidad_visual(integer, varchar, varchar, varchar, varchar) FROM fashionstore_app;
REVOKE ALL ON PROCEDURE sp_plataforma_auditar(varchar, varchar, jsonb, inet) FROM fashionstore_app;
REVOKE ALL ON FUNCTION fn_plataforma_uso_sucursales(integer) FROM fashionstore_app;

-- ------------------------------------------------------------------
-- Rol de aprovisionamiento / plataforma: acceso privilegiado explicito.
-- ------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO fashionstore_provisioner;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO fashionstore_provisioner;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO fashionstore_provisioner;
GRANT EXECUTE ON ALL PROCEDURES IN SCHEMA public TO fashionstore_provisioner;

-- Resolucion de codigo->empresa antes del contexto de tenant: la ejecuta el
-- rol HTTP, pero su propietario debe poder leer sin RLS.
ALTER FUNCTION auth_resolver_empresa(varchar) OWNER TO fashionstore_provisioner;
GRANT EXECUTE ON FUNCTION auth_resolver_empresa(varchar) TO fashionstore_app;

-- Verificacion inmediata de atributos criticos.
DO $$
DECLARE v_bypass boolean; v_super boolean; v_owner boolean;
BEGIN
  SELECT rolbypassrls, rolsuper INTO v_bypass, v_super FROM pg_roles WHERE rolname='fashionstore_app';
  IF v_bypass OR v_super THEN
    RAISE EXCEPTION 'fashionstore_app no puede tener BYPASSRLS ni SUPERUSER';
  END IF;
  SELECT rolbypassrls INTO v_bypass FROM pg_roles WHERE rolname='fashionstore_provisioner';
  IF NOT v_bypass THEN
    RAISE EXCEPTION 'fashionstore_provisioner requiere BYPASSRLS explicito para aprovisionar';
  END IF;
  SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_roles r ON r.oid=c.relowner
                 WHERE c.relname='empresa' AND r.rolname='fashionstore_app') INTO v_owner;
  IF v_owner THEN
    RAISE EXCEPTION 'fashionstore_app no puede ser propietario de las tablas';
  END IF;
END $$;
