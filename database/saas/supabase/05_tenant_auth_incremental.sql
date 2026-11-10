-- Fase pendiente de autorización: contexto tenant y resolución de empresa.
-- No crear/aplicar en Supabase sin roles y respaldo recuperable aprobados.
-- No modifica datos comerciales ni crea empresas.
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
    RAISE EXCEPTION 'fashionstore_app debe existir y no tener BYPASSRLS';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='fashionstore_provisioner' AND rolbypassrls) THEN
    RAISE EXCEPTION 'fashionstore_provisioner debe existir con BYPASSRLS';
  END IF;
END $$;

CREATE OR REPLACE FUNCTION public.fn_idemp_actual() RETURNS integer
LANGUAGE sql STABLE AS $$
  SELECT NULLIF(current_setting('app.idemp', true), '')::integer;
$$;

CREATE OR REPLACE FUNCTION public.fn_exigir_tenant(p_idemp integer) RETURNS void
LANGUAGE plpgsql STABLE AS $$
BEGIN
  IF p_idemp IS NULL OR public.fn_idemp_actual() IS DISTINCT FROM p_idemp THEN
    RAISE EXCEPTION 'Tenant no autorizado o contexto tenant ausente';
  END IF;
END;
$$;

CREATE OR REPLACE FUNCTION public.auth_resolver_empresa(p_codigo varchar) RETURNS integer
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT e.idemp FROM public.empresa e
  WHERE e.codigo=lower(btrim(p_codigo)) AND e.estado='activa'
    AND EXISTS (SELECT 1 FROM public.suscripcion s
                WHERE s.idemp=e.idemp AND s.estado='activa'
                  AND current_date BETWEEN s.fechainicio AND s.fechavencimiento);
$$;
-- Nota Supabase: no se puede ALTER FUNCTION OWNER a fashionstore_provisioner
-- sin ser superusuario. La función queda SECURITY DEFINER owned por el creador
-- (postgres.vleqtsrmaqvslyugurzj), quien tiene acceso a empresa/suscripcion.
-- Se concede EXECUTE solo a fashionstore_app.
GRANT SELECT ON public.empresa, public.suscripcion TO fashionstore_provisioner;
REVOKE ALL ON FUNCTION public.auth_resolver_empresa(varchar) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.auth_resolver_empresa(varchar) TO fashionstore_app;

COMMIT;
