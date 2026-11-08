# Migraciones y transición SaaS

La carpeta `migrations/` documenta el parche histórico de CU09. La guía vigente
de esquema SaaS, orden de ejecución, roles, datos demo, sincronización y límites
de migración de datos está en [`../database/saas/README.md`](../database/saas/README.md).

Los SQL en `database/saas/` son la copia canónica versionada
para una base PostgreSQL nueva y separada más
parches sobre ese esquema. No migran datos de la base actual, no deben ejecutarse
contra Supabase y no se han usado para alterar el origen. Los datos de ejemplo
son responsabilidad del seed idempotente del backend; no confundirlos con una
migración.

## Estado verificado

- El cluster de pruebas configurado en `127.0.0.1:55432` es independiente de la
  configuración de base del proyecto.
- El script `05_verificacion_rls.sql` se ejecutó sobre ese PostgreSQL con el rol
  de aplicación, y sus 15 verificaciones PASS no sustituyen pruebas de los
  módulos que siguen bloqueados.
- Las pruebas de sync/API requieren `tests/test_saas_postgres.py`; SQLite no
  demuestra RLS.
- La integración de datos preexistentes continúa pendiente: falta asignación de
  tenant por registro, reconciliación de IDs/correos, conversión de
  `claveoperacion` a UUID y ensayo de importación/retorno con una copia.

No aplicar `001_fashionstore_saas_cu09.sql` como si fuese migración de datos ni
como reemplazo de 01–04. Consultar la guía SaaS para comandos y precondiciones.

La entrega incluye también la migración canónica de versiones de planes en
`app/modules/saas_suscripciones/planes_funcionalidades/migrations/001_versiones_planes.sql`.
Se aplica después de 03 y antes de 04 según la guía enlazada; no mantener una
segunda copia en esta carpeta. CU08 no agrega DDL ni migra auditoría histórica.
