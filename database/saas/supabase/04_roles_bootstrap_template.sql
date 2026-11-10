-- Plantilla remota: NO ejecutar sin autorización explícita de bootstrap.
-- Requiere psql y las variables de entorno FASHIONSTORE_APP_PASSWORD,
-- FASHIONSTORE_PROVISIONER_PASSWORD y FASHIONSTORE_BACKUP_PASSWORD. Ninguna
-- contraseña se versiona ni se escribe en esta migración.
\set ON_ERROR_STOP on
\if :{?target_db}
\else
  \echo 'Falta -v target_db=<base esperada>; no se creó ningún rol.'
  \quit 3
\endif
\getenv fashionstore_app_password FASHIONSTORE_APP_PASSWORD
\getenv fashionstore_provisioner_password FASHIONSTORE_PROVISIONER_PASSWORD
\getenv fashionstore_backup_password FASHIONSTORE_BACKUP_PASSWORD
\if :{?fashionstore_app_password}
\else
  \echo 'Falta FASHIONSTORE_APP_PASSWORD; no se creó ningún rol.'
  \quit 3
\endif
\if :{?fashionstore_provisioner_password}
\else
  \echo 'Falta FASHIONSTORE_PROVISIONER_PASSWORD; no se creó ningún rol.'
  \quit 3
\endif
\if :{?fashionstore_backup_password}
\else
  \echo 'Falta FASHIONSTORE_BACKUP_PASSWORD; no se creó ningún rol.'
  \quit 3
\endif
BEGIN;
SELECT set_config('fashionstore.expected_migration_db', :'target_db', true);
DO $$
BEGIN
  IF current_database() <> current_setting('fashionstore.expected_migration_db') THEN
    RAISE EXCEPTION 'Destino inesperado';
  END IF;
  IF current_user IN ('anon','authenticated','service_role') THEN
    RAISE EXCEPTION 'La conexión de bootstrap no puede usar un rol expuesto por Supabase';
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='fashionstore_owner') THEN
    CREATE ROLE fashionstore_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='fashionstore_app') THEN
    CREATE ROLE fashionstore_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='fashionstore_provisioner') THEN
    CREATE ROLE fashionstore_provisioner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION BYPASSRLS NOINHERIT;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='fashionstore_backup') THEN
    CREATE ROLE fashionstore_backup LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION BYPASSRLS NOINHERIT;
  END IF;
END $$;
ALTER ROLE fashionstore_app PASSWORD :'fashionstore_app_password';
ALTER ROLE fashionstore_provisioner PASSWORD :'fashionstore_provisioner_password';
ALTER ROLE fashionstore_backup PASSWORD :'fashionstore_backup_password';
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='fashionstore_app' AND rolcanlogin AND NOT rolsuper AND NOT rolbypassrls AND NOT rolinherit)
     OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='fashionstore_provisioner' AND rolcanlogin AND NOT rolsuper AND rolbypassrls AND NOT rolinherit)
     OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='fashionstore_backup' AND rolcanlogin AND NOT rolsuper AND rolbypassrls AND NOT rolinherit)
     OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='fashionstore_owner' AND NOT rolcanlogin AND NOT rolsuper AND NOT rolbypassrls) THEN
    RAISE EXCEPTION 'Atributos de roles FashionStore incompatibles; detener la migración';
  END IF;
END $$;
COMMIT;
