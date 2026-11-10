-- Solo lectura: inventario del esquema compartido antes de preparar/aplicar DDL.
-- Ejecutar con psql en Supabase. No modifica objetos ni lee filas de negocio.
\set ON_ERROR_STOP on
BEGIN READ ONLY;

SELECT current_database() AS base, current_user AS rol_actual,
       inet_server_addr() AS servidor, inet_server_port() AS puerto;

SELECT rolname, rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, rolcanlogin
FROM pg_roles
WHERE rolname IN ('postgres', 'anon', 'authenticated', 'service_role',
                  'fashionstore_owner', 'fashionstore_app',
                  'fashionstore_platform', 'fashionstore_provisioner',
                  'fashionstore_backup', 'fashionstore_expiry')
ORDER BY rolname;

SELECT c.relkind, count(*) AS cantidad
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind IN ('r','p','v','m','S')
GROUP BY c.relkind ORDER BY c.relkind;

SELECT table_name, column_name, data_type, udt_name,
       character_maximum_length, numeric_precision, numeric_scale,
       is_nullable, column_default, is_identity
FROM information_schema.columns
WHERE table_schema = 'public'
ORDER BY table_name, ordinal_position;

SELECT c.relname AS tabla, co.contype AS tipo,
       pg_get_constraintdef(co.oid, true) AS definicion
FROM pg_constraint co
JOIN pg_class c ON c.oid = co.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public'
ORDER BY c.relname, co.contype, co.conname;

SELECT t.relname AS tabla, i.relname AS indice,
       pg_get_indexdef(i.oid) AS definicion
FROM pg_index x
JOIN pg_class t ON t.oid = x.indrelid
JOIN pg_namespace n ON n.oid = t.relnamespace
JOIN pg_class i ON i.oid = x.indexrelid
WHERE n.nspname = 'public'
ORDER BY t.relname, i.relname;

SELECT s.relname AS secuencia, t.relname AS tabla, a.attname AS columna,
       d.deptype AS dependencia
FROM pg_class s
JOIN pg_namespace n ON n.oid = s.relnamespace
LEFT JOIN pg_depend d ON d.objid = s.oid AND d.deptype IN ('a','i')
LEFT JOIN pg_class t ON t.oid = d.refobjid
LEFT JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = d.refobjsubid
WHERE n.nspname = 'public' AND s.relkind = 'S'
ORDER BY s.relname;

SELECT r.relname AS tabla, t.tgname AS trigger,
       t.tgenabled AS estado, pg_get_triggerdef(t.oid, true) AS definicion
FROM pg_trigger t
JOIN pg_class r ON r.oid = t.tgrelid
JOIN pg_namespace n ON n.oid = r.relnamespace
WHERE n.nspname = 'public' AND NOT t.tgisinternal
ORDER BY r.relname, t.tgname;

SELECT p.prokind AS tipo, p.proname AS rutina,
       pg_get_function_identity_arguments(p.oid) AS argumentos,
       p.prosecdef AS security_definer,
       pg_get_functiondef(p.oid) AS definicion
FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = 'public'
ORDER BY p.proname, argumentos;

SELECT tablename, policyname, roles, cmd, qual, with_check
FROM pg_policies WHERE schemaname = 'public'
ORDER BY tablename, policyname;

SELECT c.relname AS tabla, c.relrowsecurity AS rls,
       c.relforcerowsecurity AS force_rls, r.rolname AS propietario
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_roles r ON r.oid = c.relowner
WHERE n.nspname = 'public' AND c.relkind IN ('r','p')
ORDER BY c.relname;

SELECT grantee, table_name, privilege_type, is_grantable
FROM information_schema.role_table_grants
WHERE table_schema = 'public'
ORDER BY grantee, table_name, privilege_type;

ROLLBACK;
