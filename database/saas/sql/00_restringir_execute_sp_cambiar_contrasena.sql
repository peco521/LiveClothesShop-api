-- ============================================================================
-- Script: 00_restringir_execute_sp_cambiar_contrasena.sql
-- Descripción: Restringe el acceso a sp_cambiar_contrasena para evitar que
--              usuarios no autorizados cambien contraseñas directamente.
--
-- Propósito: Eliminar EXECUTE de PUBLIC y otros roles no autorizados,
--              manteniendo el acceso solo para roles de administración autorizados.
--
-- Uso: Ejecutar como superusuario (postgres) en la base de producción.
--
-- ADVERTENCIA: Ejecutar SOLO después de desplegar el nuevo código en
-- Render, ya que el backend ya no usa este procedimiento.
-- ============================================================================

-- ------------------------------------------------------------
-- PASO 1: Verificar estado actual (solo lectura)
-- ------------------------------------------------------------
SELECT 'ANTES - Permisos de sp_cambiar_contrasena:' AS info;
SELECT grantee, privilege_type
FROM information_schema.role_routine_grants
WHERE routine_name = 'sp_cambiar_contrasena'
ORDER BY grantee;

-- ------------------------------------------------------------
-- PASO 2: Revocar EXECUTE de roles que no deben tener acceso directo
-- ------------------------------------------------------------
DO $$
BEGIN
    REVOKE EXECUTE ON PROCEDURE public.sp_cambiar_contrasena(integer, character varying, character varying) FROM PUBLIC;
EXCEPTION WHEN others THEN
    RAISE NOTICE 'No se pudo revocar de PUBLIC: %', SQLERRM;
END $$;

DO $$
BEGIN
    REVOKE EXECUTE ON PROCEDURE public.sp_cambiar_contrasena(integer, character varying, character varying) FROM anon;
EXCEPTION WHEN others THEN
    RAISE NOTICE 'No se pudo revocar de anon: %', SQLERRM;
END $$;

DO $$
BEGIN
    REVOKE EXECUTE ON PROCEDURE public.sp_cambiar_contrasena(integer, character varying, character varying) FROM authenticated;
EXCEPTION WHEN others THEN
    RAISE NOTICE 'No se pudo revocar de authenticated: %', SQLERRM;
END $$;

DO $$
BEGIN
    REVOKE EXECUTE ON PROCEDURE public.sp_cambiar_contrasena(integer, character varying, character varying) FROM service_role;
EXCEPTION WHEN others THEN
    RAISE NOTICE 'No se pudo revocar de service_role: %', SQLERRM;
END $$;

DO $$
BEGIN
    REVOKE EXECUTE ON PROCEDURE public.sp_cambiar_contrasena(integer, character varying, character varying) FROM fashionstore_app;
EXCEPTION WHEN others THEN
    RAISE NOTICE 'No se pudo revocar de fashionstore_app: %', SQLERRM;
END $$;

-- ------------------------------------------------------------
-- PASO 3: Conceder EXECUTE solo al rol de plataforma/aprovisionamiento
--         fashionstore_app ya NO necesita este permiso (código actualizado)
--         fashionstore_provisioner sí lo necesita para restablecimiento administrativo
-- ------------------------------------------------------------
DO $$
BEGIN
    GRANT EXECUTE ON PROCEDURE public.sp_cambiar_contrasena(integer, character varying, character varying) TO fashionstore_provisioner;
EXCEPTION WHEN others THEN
    RAISE NOTICE 'No se pudo conceder a fashionstore_provisioner: %', SQLERRM;
END $$;

-- ------------------------------------------------------------
-- PASO 4: Verificación final (solo lectura)
-- ------------------------------------------------------------
SELECT 'DESPUÉS - Permisos de sp_cambiar_contrasena:' AS info;
SELECT grantee, privilege_type
FROM information_schema.role_routine_grants
WHERE routine_name = 'sp_cambiar_contrasena'
ORDER BY grantee;

-- ------------------------------------------------------------
-- PASO 5: Verificación adicional: comprobar que el procedimiento existe
-- ------------------------------------------------------------
SELECT routine_name, routine_type, security_invoker
FROM information_schema.routines
WHERE routine_name = 'sp_cambiar_contrasena';

-- ============================================================================
-- REVERSIÓN (solo si es necesario)
-- ============================================================================
-- GRANT EXECUTE ON PROCEDURE public.sp_cambiar_contrasena(integer, character varying, character varying) TO PUBLIC;
-- GRANT EXECUTE ON PROCEDURE public.sp_cambiar_contrasena(integer, character varying, character varying) TO anon;
-- GRANT EXECUTE ON PROCEDURE public.sp_cambiar_contrasena(integer, character varying, character varying) FROM authenticated;
-- GRANT EXECUTE ON PROCEDURE public.sp_cambiar_contrasena(integer, character varying, character varying) FROM service_role;
-- GRANT EXECUTE ON PROCEDURE public.sp_cambiar_contrasena(integer, character varying, character varying) TO fashionstore_app;
-- ============================================================================