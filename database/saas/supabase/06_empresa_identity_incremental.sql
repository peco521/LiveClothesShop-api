-- Fase pendiente de autorización: atributos opcionales ya usados por FastAPI.
-- Solo añade columnas NULL/default y constraints; no cambia empresa piloto ni branding.
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
  IF to_regclass('public.empresa') IS NULL THEN
    RAISE EXCEPTION 'Falta public.empresa';
  END IF;
END $$;

ALTER TABLE public.empresa ADD COLUMN IF NOT EXISTS logourl varchar(255);
ALTER TABLE public.empresa ADD COLUMN IF NOT EXISTS colorprimario varchar(7);
ALTER TABLE public.empresa ADD COLUMN IF NOT EXISTS colorsecundario varchar(7);
ALTER TABLE public.empresa ADD COLUMN IF NOT EXISTS esdemostracion boolean NOT NULL DEFAULT false;

ALTER TABLE public.empresa DROP CONSTRAINT IF EXISTS ck_empresa_color_primario;
ALTER TABLE public.empresa ADD CONSTRAINT ck_empresa_color_primario
  CHECK (colorprimario IS NULL OR colorprimario ~ '^#[0-9A-Fa-f]{6}$');
ALTER TABLE public.empresa DROP CONSTRAINT IF EXISTS ck_empresa_color_secundario;
ALTER TABLE public.empresa ADD CONSTRAINT ck_empresa_color_secundario
  CHECK (colorsecundario IS NULL OR colorsecundario ~ '^#[0-9A-Fa-f]{6}$');

COMMIT;
