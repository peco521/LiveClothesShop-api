-- Preflight específico para la migración SaaS del proyecto Supabase compartido.
-- Solo lectura: no crea roles, tablas, políticas, cuentas, planes ni empresas.
-- Uso: psql ... -v target_db=postgres -f 03_shared_saas_preflight_readonly.sql
\set ON_ERROR_STOP on
\if :{?target_db}
\else
  \echo 'Falta -v target_db=<base esperada>; no se ejecutó el preflight.'
  \quit 3
\endif

BEGIN READ ONLY;

-- psql no puede parametrizar SET LOCAL directamente en todas sus versiones.
-- Se guarda el destino esperado en una variable transaccional de la sesión.
SELECT set_config('fashionstore.expected_migration_db', :'target_db', true);

DO $$
BEGIN
  IF current_database() <> current_setting('fashionstore.expected_migration_db', true) THEN
    RAISE EXCEPTION 'Destino inesperado; no se inspeccionó la base';
  END IF;
END $$;

SELECT current_database() AS base, current_user AS rol,
       inet_server_addr() AS servidor, inet_server_port() AS puerto;

SELECT idplan, nombre, maxsucursales, estado
FROM public.plan ORDER BY idplan;

SELECT e.idemp, e.codigo, e.nombre, e.estado,
       count(DISTINCT s.idsusc) FILTER (WHERE s.estado='activa') AS suscripciones_activas,
       count(DISTINCT su.nro) FILTER (WHERE su.estado='activo') AS sucursales_activas,
       count(DISTINCT u.idusuario) AS usuarios
FROM public.empresa e
LEFT JOIN public.suscripcion s ON s.idemp=e.idemp
LEFT JOIN public.sucursal su ON su.idemp=e.idemp
LEFT JOIN public.usuario u ON u.idemp=e.idemp
GROUP BY e.idemp,e.codigo,e.nombre,e.estado
ORDER BY e.idemp;

SELECT codigo, nombre
FROM public.empresa
WHERE lower(codigo) IN ('manaco','kosy-pantalones','urbanwomen','kosy-prendas','lupo')
ORDER BY codigo;

SELECT relname AS tabla_sin_rls
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname='public' AND c.relkind='r' AND NOT c.relrowsecurity
ORDER BY relname;

SELECT tablename, policyname, roles, cmd
FROM pg_policies WHERE schemaname='public'
ORDER BY tablename,policyname;

SELECT
  to_regprocedure('public.auth_resolver_empresa(varchar)') IS NOT NULL AS auth_resolver_empresa,
  to_regprocedure('public.fn_idemp_actual()') IS NOT NULL AS fn_idemp_actual,
  to_regclass('public.plan_version') IS NOT NULL AS plan_version,
  EXISTS (SELECT 1 FROM information_schema.columns
          WHERE table_schema='public' AND table_name='empresa' AND column_name='logourl') AS empresa_logourl,
  EXISTS (SELECT 1 FROM information_schema.columns
          WHERE table_schema='public' AND table_name='empresa' AND column_name='esdemostracion') AS empresa_esdemostracion;

SELECT c.relname AS tabla,
       count(*) FILTER (WHERE a.attname='idemp') > 0 AS tiene_idemp,
       c.relrowsecurity AS rls, c.relforcerowsecurity AS force_rls
FROM pg_class c
JOIN pg_namespace n ON n.oid=c.relnamespace
LEFT JOIN pg_attribute a ON a.attrelid=c.oid AND a.attname='idemp' AND NOT a.attisdropped
WHERE n.nspname='public' AND c.relkind='r'
GROUP BY c.relname,c.relrowsecurity,c.relforcerowsecurity
ORDER BY c.relname;

ROLLBACK;
