-- Separa fixtures E2E de productos comerciales. No clasifica datos históricos:
-- hacerlo exige revisión explícita de cada fila y autorización de datos.
\set ON_ERROR_STOP on
\if :{?target_db}
\else
  \echo 'Falta -v target_db=<base esperada>; no se aplicó la migración.'
  \quit 3
\endif
BEGIN;
SELECT set_config('fashionstore.expected_migration_db', :'target_db', true);
DO $$ BEGIN
  IF current_database() <> current_setting('fashionstore.expected_migration_db') THEN
    RAISE EXCEPTION 'Destino inesperado';
  END IF;
END $$;
ALTER TABLE public.producto ADD COLUMN IF NOT EXISTS esprueba boolean NOT NULL DEFAULT false;
CREATE INDEX IF NOT EXISTS ix_producto_catalogo_comercial
  ON public.producto(idemp, descripcion, idprod)
  WHERE estado='activo' AND esprueba=false;
COMMIT;
