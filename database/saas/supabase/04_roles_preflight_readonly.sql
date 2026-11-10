-- Solo lectura. Confirma si el rol ejecutor puede preparar las identidades
-- FashionStore sin modificar roles existentes ni sus credenciales.
\set ON_ERROR_STOP on
\if :{?target_db}
\else
  \echo 'Falta -v target_db=<base esperada>; no se ejecutó el preflight.'
  \quit 3
\endif
BEGIN READ ONLY;
SELECT set_config('fashionstore.expected_migration_db', :'target_db', true);
DO $$
BEGIN
  IF current_database() <> current_setting('fashionstore.expected_migration_db') THEN
    RAISE EXCEPTION 'Destino inesperado';
  END IF;
END $$;

SELECT current_user, rolsuper, rolcreaterole, rolbypassrls
FROM pg_roles WHERE rolname=current_user;

SELECT rolname, rolcanlogin, rolsuper, rolbypassrls, rolinherit
FROM pg_roles
WHERE rolname IN ('fashionstore_owner','fashionstore_app','fashionstore_provisioner','fashionstore_backup')
ORDER BY rolname;

SELECT has_schema_privilege(current_user, 'public', 'CREATE') AS puede_crear_en_public,
       has_database_privilege(current_user, current_database(), 'CREATE') AS puede_crear_en_base;

ROLLBACK;
