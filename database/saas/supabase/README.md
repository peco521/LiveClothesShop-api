# Paquete de revisión Supabase

- `00_catalogo_preflight_readonly.sql`: catálogo solamente; termina en `ROLLBACK`.
- `01_postgrest_revocations_dryrun.sql`: borrador acotado de permisos; termina en `ROLLBACK` y no es cambio persistente.
- `02_tenant_rls_incremental.sql`: fase RLS ensayada solo en `fashionstore_migration_test`; no se aplicó en Supabase.
- Ningún archivo de esta carpeta es la migración de esquema final. No ejecutar en Supabase hasta aprobar consumidores, roles y estrategia de staging.
- El plan, hallazgos, decisión tenant y propuesta de ensayo están en [`../../../docs/SUPABASE_INTEGRATION_REVIEW.md`](../../../docs/SUPABASE_INTEGRATION_REVIEW.md).
