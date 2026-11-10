-- BORRADOR DE REVISION. No aplicar como cambio persistente.
-- Revoca solo el acceso directo PostgREST de anon/authenticated a seis tablas
-- FashionStore que hoy no tienen RLS y a sus secuencias serial asociadas.
-- No toca tablas Django, roles, rutinas, policies ni grants de otros roles.
-- La transacción termina en ROLLBACK; no cambia el estado al ejecutarse.
--
-- Búsqueda de consumidores en API/Web/Mobile no encontró cliente Supabase,
-- URL /rest/v1 ni SDK PostgREST. Todos los clientes llaman la API FastAPI (/api).
-- Pendiente confirmar que no haya consumidores externos fuera de estos repos.
-- Para adoptar el cambio, revisar consumidores, agregar grants mínimos al rol
-- HTTP de backend y cambiar ROLLBACK por COMMIT en un change request aprobado.

\set ON_ERROR_STOP on
BEGIN;

DO $$
BEGIN
  IF current_database() <> 'postgres' THEN
    RAISE EXCEPTION 'Destino inesperado; se requiere revisión explícita del target';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon')
     OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
    RAISE EXCEPTION 'No existen los roles Supabase esperados';
  END IF;
  IF (SELECT count(*) FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
      WHERE n.nspname = 'public'
        AND c.relname IN ('empresa','operacion_sync','reporte_solicitud',
                          'reporte_tipo','cambio','detallecambio')
        AND c.relkind = 'r' AND NOT c.relrowsecurity) <> 6 THEN
    RAISE EXCEPTION 'Cambió el preflight: las seis tablas no-RLS no coinciden';
  END IF;
END $$;

REVOKE ALL PRIVILEGES ON TABLE
  public.empresa, public.operacion_sync, public.reporte_solicitud,
  public.reporte_tipo, public.cambio, public.detallecambio
FROM anon, authenticated;

REVOKE ALL PRIVILEGES ON SEQUENCE
  public.empresa_idemp_seq,
  public.reporte_solicitud_idsolicitud_seq,
  public.cambio_nrocambio_seq
FROM anon, authenticated;

-- Postcondition del dry-run: los roles API públicos ya no tendrían estos
-- privilegios si se confirmara el mismo script con COMMIT.
SELECT c.relname AS tabla,
       has_table_privilege('anon', c.oid, 'SELECT') AS anon_select,
       has_table_privilege('anon', c.oid, 'INSERT') AS anon_insert,
       has_table_privilege('authenticated', c.oid, 'SELECT') AS auth_select,
       has_table_privilege('authenticated', c.oid, 'INSERT') AS auth_insert
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public'
  AND c.relname IN ('empresa','operacion_sync','reporte_solicitud',
                    'reporte_tipo','cambio','detallecambio')
ORDER BY c.relname;

SELECT 'empresa_idemp_seq' AS secuencia,
       has_sequence_privilege('anon','public.empresa_idemp_seq','USAGE') AS anon_usage,
       has_sequence_privilege('authenticated','public.empresa_idemp_seq','USAGE') AS auth_usage
UNION ALL SELECT 'reporte_solicitud_idsolicitud_seq',
       has_sequence_privilege('anon','public.reporte_solicitud_idsolicitud_seq','USAGE'),
       has_sequence_privilege('authenticated','public.reporte_solicitud_idsolicitud_seq','USAGE')
UNION ALL SELECT 'cambio_nrocambio_seq',
       has_sequence_privilege('anon','public.cambio_nrocambio_seq','USAGE'),
       has_sequence_privilege('authenticated','public.cambio_nrocambio_seq','USAGE');

ROLLBACK;
