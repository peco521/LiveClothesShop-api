-- Habilita la autorización CU11 que exige /api/sync para reservas offline.
-- No crea empresas ni modifica reservas, ventas, inventario ni productos.
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

INSERT INTO public.funcion(id, descripcion)
VALUES ('CU11', 'Gestionar Reserva')
ON CONFLICT (id) DO NOTHING;

-- Solo roles activos que pertenecen exclusivamente a clientes existentes.
-- Evita conceder CU11 a roles compartidos con personal o administradores.
INSERT INTO public.rol_funcion(nrorol, idfun, descripcion, idemp)
SELECT r.nro, 'CU11', 'Gestionar Reserva', r.idemp
FROM public.rol r
WHERE r.estado = 'activo'
  AND EXISTS (
    SELECT 1 FROM public.usuario u
    WHERE u.idemp = r.idemp AND u.nrorol = r.nro AND u.tipo = 'C'
  )
  AND NOT EXISTS (
    SELECT 1 FROM public.usuario u
    WHERE u.idemp = r.idemp AND u.nrorol = r.nro AND u.tipo <> 'C'
  )
ON CONFLICT DO NOTHING;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM public.funcion WHERE id = 'CU11') THEN
    RAISE EXCEPTION 'No se pudo registrar CU11';
  END IF;
END $$;
COMMIT;
