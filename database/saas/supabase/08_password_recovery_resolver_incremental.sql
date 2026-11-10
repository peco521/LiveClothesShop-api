-- Resuelve el tenant de una recuperación antes de establecer app.idemp.
-- No revela el digest ni permite leer otras recuperaciones.
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
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='fashionstore_app' AND NOT rolsuper AND NOT rolbypassrls)
     OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='fashionstore_provisioner' AND rolbypassrls) THEN
    RAISE EXCEPTION 'Roles FashionStore incompatibles';
  END IF;
END $$;
CREATE OR REPLACE FUNCTION public.sp_recuperacion_token_por_digest(p_token_digest varchar)
RETURNS TABLE (idemp integer, usuario_id text)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
  SELECT rc.idemp, rc.usuario_id::text
  FROM public.recuperacion_contrasena rc
  WHERE rc.token_digest = p_token_digest
$$;
ALTER FUNCTION public.sp_recuperacion_token_por_digest(varchar) OWNER TO fashionstore_provisioner;
GRANT SELECT ON public.recuperacion_contrasena TO fashionstore_provisioner;
REVOKE ALL ON FUNCTION public.sp_recuperacion_token_por_digest(varchar) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.sp_recuperacion_token_por_digest(varchar) TO fashionstore_app;
COMMIT;
