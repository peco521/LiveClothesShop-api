-- ==========================================================================
-- FashionStore - VERIFICACION DEL AISLAMIENTO POR EMPRESA (RLS real)
-- ==========================================================================
-- Este script NO crea ni migra nada. Comprueba, sobre la base DE PRUEBAS
-- (127.0.0.1:55432), que el rol HTTP `fashionstore_app`:
--
--   1. Solo ve filas de la empresa fijada en `app.idemp`.
--   2. No puede leer, actualizar ni borrar filas de otra empresa.
--   3. No puede insertar filas con otro idEmp (WITH CHECK).
--   4. No puede saltarse las politicas con SET row_security = off.
--   5. No es propietario de tablas ni puede crear objetos en `public`.
--   6. No puede ejecutar rutinas de plataforma.
--   7. El contexto de tenant desaparece al terminar la transaccion.
--   8. El limite de sucursales del plan se aplica tambien con un INSERT
--      directo, no solo desde la API.
--   9. Las versiones efectivas se leen por tenant, sin permitir mutaciones HTTP.
--
-- Requisitos:
--   * database/saas/01, 02, 03, migration 001 de versiones y 04 ya aplicados.
--   * scripts/seed_demo.py ejecutado (empresas manaco y kosy-pantalones).
--   * Ejecutar como superusuario local (postgres) con psql:
--
--       psql -h 127.0.0.1 -p 55432 -U postgres -d fashionstore_test \
--            -v ON_ERROR_STOP=1 -f database/saas/05_verificacion_rls.sql
--
-- Las pruebas que esperan un error lo capturan y lo informan como PASS. Cual
-- desviacion lanza una excepcion y detiene el script (ON_ERROR_STOP=1).
-- NOTA: PostgreSQL real es la unica prueba valida de RLS. SQLite no aplica
-- politicas de fila y no se usa aqui.
-- ==========================================================================

\set ON_ERROR_STOP on

BEGIN;

-- --------------------------------------------------------------------------
-- 0) Datos de referencia (lectura como superusuario, antes de cambiar rol)
-- --------------------------------------------------------------------------
SELECT (SELECT idemp FROM empresa WHERE codigo = 'manaco')          AS emp_a,
       (SELECT idemp FROM empresa WHERE codigo = 'kosy-pantalones') AS emp_b,
       (SELECT idemp FROM empresa WHERE codigo = 'kosy-prendas')    AS emp_c
\gset

\if :{?emp_a}
\else
  \echo 'ERROR: ejecute primero scripts/seed_demo.py (faltan empresas de demostracion)'
  ROLLBACK;
  \quit 1
\endif

SELECT set_config('app.idemp', :'emp_a', true)      AS contexto_empresa_a,
       set_config('app.verif_otra', :'emp_b', true) AS empresa_b_para_verificar,
       set_config('app.verif_ciudad_a',
                  (SELECT min(id)::text FROM ciudad WHERE idemp = :emp_a), true),
       set_config('app.verif_nro_a',
                  (SELECT (coalesce(max(nro), 0) + 1)::text FROM sucursal WHERE idemp = :emp_a), true),
       set_config('app.verif_ciudad_c',
                  (SELECT min(id)::text FROM ciudad WHERE idemp = :emp_c), true),
       set_config('app.verif_nro_c',
                  (SELECT (coalesce(max(nro), 0) + 1)::text FROM sucursal WHERE idemp = :emp_c), true);

SELECT 'Empresas de prueba: A=' || :'emp_a' || ' B=' || :'emp_b' || ' C=' || :'emp_c' AS referencia;

-- --------------------------------------------------------------------------
-- 1..8) Comprobaciones con el rol HTTP de empresa
-- --------------------------------------------------------------------------
SET LOCAL ROLE fashionstore_app;

SELECT session_user AS session_user, current_user AS current_user,
       current_setting('role') AS rol_efectivo,
       r.rolcanlogin, r.rolinherit, r.rolsuper, r.rolcreatedb,
       r.rolcreaterole, r.rolreplication, r.rolbypassrls,
       count(c.oid) FILTER (WHERE c.relowner = r.oid) AS tablas_public_propias
FROM pg_roles r
LEFT JOIN pg_class c ON c.relnamespace = 'public'::regnamespace
                     AND c.relkind IN ('r', 'p', 'v', 'm')
WHERE r.rolname = current_user
GROUP BY r.oid, r.rolcanlogin, r.rolinherit, r.rolsuper, r.rolcreatedb,
         r.rolcreaterole, r.rolreplication, r.rolbypassrls;

DO $$
DECLARE
  v_a      integer := current_setting('app.idemp')::integer;
  v_b      integer := current_setting('app.verif_otra')::integer;
  v_rows   integer;
  v_ok     boolean;
  v_expected integer;
  v_actual integer;
BEGIN
  IF current_user <> 'fashionstore_app'
     OR NOT EXISTS (
       SELECT 1 FROM pg_roles
       WHERE rolname = current_user AND rolcanlogin
         AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole
         AND NOT rolreplication AND NOT rolbypassrls
     ) THEN
    RAISE EXCEPTION 'FALLO: las aserciones no se ejecutan como fashionstore_app restringido';
  END IF;
  IF EXISTS (
    SELECT 1 FROM pg_class c
    WHERE c.relnamespace = 'public'::regnamespace
      AND c.relkind IN ('r', 'p', 'v', 'm')
      AND c.relowner = (SELECT oid FROM pg_roles WHERE rolname = current_user)
  ) THEN
    RAISE EXCEPTION 'FALLO: fashionstore_app es propietario de relaciones en public';
  END IF;

  -- 1) Positivo: ve sus propias filas.
  SELECT count(*) INTO v_rows FROM producto WHERE idemp = v_a;
  IF v_rows <= 0 THEN
    RAISE EXCEPTION 'FALLO 1: la empresa % no ve su propio catalogo', v_a;
  END IF;
  RAISE NOTICE 'PASS 1: la empresa % ve % productos propios', v_a, v_rows;

  -- 1b) Negativo: ninguna fila de otra empresa es visible.
  SELECT count(*) INTO v_rows FROM producto WHERE idemp <> v_a;
  IF v_rows <> 0 THEN
    RAISE EXCEPTION 'FALLO 1b: se ven % filas de otras empresas', v_rows;
  END IF;
  SELECT count(*) INTO v_rows FROM producto WHERE idemp = v_b;
  IF v_rows <> 0 THEN
    RAISE EXCEPTION 'FALLO 1b: se ven % productos de la empresa %', v_rows, v_b;
  END IF;
  RAISE NOTICE 'PASS 1b: lectura cruzada bloqueada (empresa % invisible)', v_b;

  -- 1c) La tabla empresa tambien esta aislada.
  SELECT count(*) INTO v_rows FROM empresa;
  IF v_rows <> 1 THEN
    RAISE EXCEPTION 'FALLO 1c: la empresa ve % filas en empresa (esperado 1)', v_rows;
  END IF;
  RAISE NOTICE 'PASS 1c: solo es visible la propia fila de empresa';

  -- 1d) El administrador de plataforma (idEmp NULL) no es visible.
  SELECT count(*) INTO v_rows FROM usuario WHERE idemp IS NULL;
  IF v_rows <> 0 THEN
    RAISE EXCEPTION 'FALLO 1d: se ve la cuenta de plataforma (idEmp NULL)';
  END IF;
  RAISE NOTICE 'PASS 1d: la cuenta de plataforma (idEmp NULL) no es visible';

  -- 1e) La configuración efectiva usa la versión asignada a esta empresa.
  SELECT count(*) INTO v_expected FROM suscripcion WHERE idemp=v_a;
  SELECT count(*) INTO v_rows
  FROM suscripcion_plan_version sv
  JOIN suscripcion s ON s.idsusc=sv.idsusc AND s.idplan=sv.idplan
  WHERE s.idemp=v_a;
  IF v_rows <> v_expected OR v_rows = 0 THEN
    RAISE EXCEPTION 'FALLO 1e: se ven % asignaciones propias para % suscripciones', v_rows, v_expected;
  END IF;
  SELECT count(*) INTO v_rows
  FROM suscripcion_plan_version sv
  JOIN suscripcion s ON s.idsusc=sv.idsusc AND s.idplan=sv.idplan
  WHERE s.idemp=v_b;
  IF v_rows <> 0 THEN
    RAISE EXCEPTION 'FALLO 1e: se ven % asignaciones de la empresa ajena', v_rows;
  END IF;
  SELECT v.maxsucursales INTO v_expected
  FROM suscripcion s
  JOIN suscripcion_plan_version sv ON sv.idsusc=s.idsusc AND sv.idplan=s.idplan
  JOIN plan_version v ON v.idversion=sv.idversion AND v.idplan=s.idplan
  WHERE s.idemp=v_a AND s.estado='activa'
    AND current_date BETWEEN s.fechainicio AND s.fechavencimiento;
  SELECT fn_max_sucursales(v_a) INTO v_actual;
  IF v_actual IS DISTINCT FROM v_expected THEN
    RAISE EXCEPTION 'FALLO 1e: limite efectivo % distinto de version %', v_actual, v_expected;
  END IF;
  IF has_table_privilege(current_user, 'suscripcion_plan_version', 'INSERT')
     OR has_table_privilege(current_user, 'suscripcion_plan_version', 'UPDATE')
     OR has_table_privilege(current_user, 'suscripcion_plan_version', 'DELETE') THEN
    RAISE EXCEPTION 'FALLO 1e: el rol HTTP puede mutar asignaciones de versiones';
  END IF;
  SELECT count(*) INTO v_rows
  FROM pg_class c
  WHERE c.relnamespace='public'::regnamespace
    AND c.relname IN ('plan_version','plan_version_funcionalidad','suscripcion_plan_version')
    AND c.relrowsecurity AND c.relforcerowsecurity;
  IF v_rows <> 3 THEN
    RAISE EXCEPTION 'FALLO 1e: RLS no esta habilitado y forzado en las 3 tablas de versiones';
  END IF;
  RAISE NOTICE 'PASS 1e: version efectiva por tenant y asignaciones de solo lectura para HTTP';

  -- 2) UPDATE cruzado: no alcanza ninguna fila.
  UPDATE producto SET descripcion = descripcion WHERE idemp = v_b;
  GET DIAGNOSTICS v_rows = ROW_COUNT;
  IF v_rows <> 0 THEN
    RAISE EXCEPTION 'FALLO 2: UPDATE cruzado afecto % filas', v_rows;
  END IF;
  RAISE NOTICE 'PASS 2: UPDATE de otra empresa no afecta filas';

  -- 2b) DELETE cruzado: no alcanza ninguna fila.
  DELETE FROM producto WHERE idemp = v_b;
  GET DIAGNOSTICS v_rows = ROW_COUNT;
  IF v_rows <> 0 THEN
    RAISE EXCEPTION 'FALLO 2b: DELETE cruzado afecto % filas', v_rows;
  END IF;
  RAISE NOTICE 'PASS 2b: DELETE de otra empresa no afecta filas';

  -- 3) INSERT con otro idEmp: rechazado por RLS o por el trigger de tenant.
  BEGIN
    INSERT INTO producto(idprod, descripcion, estado, idpromo, idcat, idmarca,
                         idcol, idprov, idemp)
    VALUES ('RLS-FALSO-A', 'Intento cruzado', 'activo', NULL,
            (SELECT min(idcat) FROM categoria),
            (SELECT min(idmarca) FROM marca),
            (SELECT min(idcol) FROM coleccion),
            (SELECT min(idprov) FROM proveedor), v_b);
    RAISE EXCEPTION 'FALLO 3: se inserto una fila con idEmp ajeno';
  EXCEPTION
    WHEN insufficient_privilege OR raise_exception THEN
      IF SQLERRM LIKE 'FALLO%' THEN RAISE; END IF;
      RAISE NOTICE 'PASS 3: INSERT con idEmp ajeno rechazado (%)', SQLERRM;
  END;

  -- 4) SET row_security = off no permite saltarse las politicas.
  BEGIN
    SET LOCAL row_security = off;
    SELECT count(*) INTO v_rows FROM producto;
    RAISE EXCEPTION 'FALLO 4: row_security=off permitio leer % filas', v_rows;
  EXCEPTION
    WHEN insufficient_privilege THEN
      RAISE NOTICE 'PASS 4: row_security=off bloqueado para el rol de aplicacion';
  END;
  -- Restaurar el comportamiento normal dentro de esta transaccion.
  SET LOCAL row_security = on;

  -- 5) No es propietario de tablas ni puede crear en el esquema public.
  SELECT count(*) INTO v_rows
  FROM pg_class c
  WHERE c.relkind = 'r'
    AND c.relnamespace = 'public'::regnamespace
    AND c.relowner = (SELECT oid FROM pg_roles WHERE rolname = current_user);
  IF v_rows <> 0 THEN
    RAISE EXCEPTION 'FALLO 5: el rol HTTP es propietario de % tablas', v_rows;
  END IF;
  IF has_schema_privilege(current_user, 'public', 'CREATE') THEN
    RAISE EXCEPTION 'FALLO 5b: el rol HTTP puede crear objetos en public';
  END IF;
  RAISE NOTICE 'PASS 5: el rol HTTP no posee tablas ni puede crear en public';

  -- 6) No puede ejecutar rutinas de plataforma.
  IF has_function_privilege(current_user,
       'sp_plataforma_auditar(character varying,character varying,jsonb,inet)', 'EXECUTE')
     OR has_function_privilege(current_user,
       'sp_registrar_empresa(character varying,character varying,character varying,smallint,date,date,character varying,character varying,character varying,character varying,character varying,character,character varying,character varying,character varying,character varying,date,integer)', 'EXECUTE')
     OR has_function_privilege(current_user, 'sp_cambiar_plan(integer,smallint,date,date)', 'EXECUTE')
     OR has_function_privilege(current_user, 'fn_plataforma_uso_sucursales(integer)', 'EXECUTE') THEN
    RAISE EXCEPTION 'FALLO 6: el rol HTTP puede ejecutar rutinas de plataforma';
  END IF;
  RAISE NOTICE 'PASS 6: rutinas de plataforma no ejecutables por el rol HTTP';

  -- 7) FK compuesta: no se puede enlazar una fila propia con un padre ajeno.
  BEGIN
    INSERT INTO inventario(nroinv, stock, cantdisp, nrosuc, idvar, idemp)
    VALUES (999000 + v_a, 1, 1,
            (SELECT min(nro) FROM sucursal),
            'KSP-V11',               -- variante de la empresa B
            v_a);
    RAISE EXCEPTION 'FALLO 7: se acepto una variante de otra empresa';
  EXCEPTION
    WHEN foreign_key_violation THEN
      RAISE NOTICE 'PASS 7: FK compuesta (idEmp,idVariante) rechaza padres de otra empresa';
    WHEN raise_exception THEN
      IF SQLERRM LIKE 'FALLO%' THEN RAISE; END IF;
      RAISE NOTICE 'PASS 7: insercion cruzada rechazada por trigger (%)', SQLERRM;
  END;

  -- 8) INSERT sin contexto de empresa: rechazado por el trigger de tenant.
  PERFORM set_config('app.idemp', '', true);
  BEGIN
    INSERT INTO sucursal(nro, nombre, direccion, estado, idciud, idemp)
    VALUES (current_setting('app.verif_nro_a')::integer, 'Sin contexto', 'Calle', 'activo',
            current_setting('app.verif_ciudad_a')::integer, v_a);
    RAISE EXCEPTION 'FALLO 8: se inserto una sucursal sin contexto de tenant';
  EXCEPTION
    WHEN raise_exception THEN
      IF SQLERRM LIKE 'FALLO%' THEN RAISE; END IF;
      IF SQLERRM NOT LIKE '%contexto de tenant%' THEN
        RAISE;
      END IF;
      RAISE NOTICE 'PASS 8: INSERT sin contexto de tenant rechazado por el trigger';
  END;
END;
$$;

-- --------------------------------------------------------------------------
-- 9) Limite de sucursales: kosy-prendas (Basico, maximo 2, ya tiene 2)
-- --------------------------------------------------------------------------
SELECT set_config('app.idemp', :'emp_c', true);

DO $$
DECLARE
  v_c integer := current_setting('app.idemp')::integer;
BEGIN
  BEGIN
    INSERT INTO sucursal(nro, nombre, direccion, estado, idciud, idemp)
    VALUES (current_setting('app.verif_nro_c')::integer, 'Sucursal excedente',
            'Calle', 'activo', current_setting('app.verif_ciudad_c')::integer, v_c);
    RAISE EXCEPTION 'FALLO 9: se supero el limite de sucursales del plan Basico';
  EXCEPTION
    WHEN raise_exception THEN
      IF SQLERRM LIKE 'FALLO%' THEN RAISE; END IF;
      IF SQLERRM NOT LIKE '%sucursales%' THEN
        RAISE;
      END IF;
      RAISE NOTICE 'PASS 9: limite de sucursales del plan aplicado por trigger (%)', SQLERRM;
  END;
END;
$$;

COMMIT;

-- --------------------------------------------------------------------------
-- 10) El contexto de tenant no sobrevive al COMMIT.
-- --------------------------------------------------------------------------
BEGIN;
SET LOCAL ROLE fashionstore_app;

DO $$
DECLARE
  v_ctx text := current_setting('app.idemp', true);
  v_rows integer;
BEGIN
  IF coalesce(v_ctx, '') <> '' THEN
    RAISE EXCEPTION 'FALLO 10: app.idemp sobrevivio al COMMIT (%)', v_ctx;
  END IF;
  SELECT count(*) INTO v_rows FROM producto;
  IF v_rows <> 0 THEN
    RAISE EXCEPTION 'FALLO 10b: sin contexto se leen % filas', v_rows;
  END IF;
  RAISE NOTICE 'PASS 10: sin contexto de tenant no se lee ninguna fila';
END;
$$;

ROLLBACK;

\echo ''
\echo '========================================================================='
\echo ' VERIFICACION RLS COMPLETADA: todas las comprobaciones PASS'
\echo '========================================================================='
