-- Columnas y constraints de CU23/CU25 (devoluciones y cambios) para Supabase.
-- Equivalente controlado de 05_cu23_devoluciones_cambios.sql, adaptado a los
-- nombres de columna del modelo ORM (snake_case). No modifica datos de empresa
-- piloto ni crea empresas. Todas las columnas son NULL o tienen default, por lo
-- que no rompen filas existentes.
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
END $$;

-- Inventario: stock dañado (requerido por el modelo ORM Inventario).
ALTER TABLE public.inventario ADD COLUMN IF NOT EXISTS stockdanado integer NOT NULL DEFAULT 0;
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'public.inventario'::regclass
      AND conname = 'ck_inventario_stock_danado'
  ) THEN
    ALTER TABLE public.inventario
      ADD CONSTRAINT ck_inventario_stock_danado CHECK (stockdanado >= 0);
  END IF;
END $$;

-- Detalle devolución: condición del artículo devuelto.
ALTER TABLE public.detalledev ADD COLUMN IF NOT EXISTS condicion varchar(20);
ALTER TABLE public.detalledev DROP CONSTRAINT IF EXISTS ck_detalledev_condicion;
ALTER TABLE public.detalledev
  ADD CONSTRAINT ck_detalledev_condicion
  CHECK (condicion IS NULL OR condicion IN ('apta', 'danada'));

-- Detalle venta: información de devolución aplicada por línea.
ALTER TABLE public.detalleventa ADD COLUMN IF NOT EXISTS condicionesdevolucionaplicadas text;
ALTER TABLE public.detalleventa ADD COLUMN IF NOT EXISTS diasdevolucionaplicado integer;
ALTER TABLE public.detalleventa ADD COLUMN IF NOT EXISTS porcentajedevolucionaplicado numeric;

-- Devolución: huella de solicitud e id de operación de sincronización.
ALTER TABLE public.devolucion ADD COLUMN IF NOT EXISTS huellasolicitud char(64);
ALTER TABLE public.devolucion ADD COLUMN IF NOT EXISTS idoperacion uuid;

-- Cambio: vínculos a devolución y venta nueva.
ALTER TABLE public.cambio ADD COLUMN IF NOT EXISTS nrodev integer;
ALTER TABLE public.cambio ADD COLUMN IF NOT EXISTS nroventanueva integer;
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'public.cambio'::regclass
      AND conname = 'fk_cambio_devolucion'
  ) THEN
    ALTER TABLE public.cambio
      ADD CONSTRAINT fk_cambio_devolucion
      FOREIGN KEY (idemp, nrodev)
      REFERENCES public.devolucion (idemp, nrodev);
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'public.cambio'::regclass
      AND conname = 'fk_cambio_venta_nueva'
  ) THEN
    ALTER TABLE public.cambio
      ADD CONSTRAINT fk_cambio_venta_nueva
      FOREIGN KEY (idemp, nroventanueva)
      REFERENCES public.venta (idemp, nroventa);
  END IF;
END $$;

-- Detalle cambio: marca si la línea está retenida.
ALTER TABLE public.detallecambio ADD COLUMN IF NOT EXISTS retenida boolean NOT NULL DEFAULT false;

-- Índices únicos para evitar duplicados de operación en devolución y cambio.
CREATE UNIQUE INDEX IF NOT EXISTS uq_devolucion_operacion
  ON public.devolucion (idemp, idoperacion)
  WHERE idoperacion IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_cambio_devolucion
  ON public.cambio (idemp, nrodev)
  WHERE nrodev IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_cambio_venta_nueva
  ON public.cambio (idemp, nroventanueva)
  WHERE nroventanueva IS NOT NULL;

COMMIT;
