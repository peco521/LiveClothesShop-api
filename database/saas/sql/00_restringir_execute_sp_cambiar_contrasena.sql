-- ============================================================================
-- Script: 00_restringir_execute_sp_cambiar_contrasena.sql
-- Descripción: Restringe EXECUTE sobre sp_cambiar_contrasena al rol de
--              aprovisionamiento autorizado.
--
-- Ejecutar SOLO después de desplegar y verificar el código que usa
-- cambiar_contrasena_propio() para login y recuperación. Ejecutar como el
-- propietario del procedimiento o un rol con privilegios equivalentes.
--
-- Seguridad operacional:
--   * La operación completa es una única transacción.
--   * Una precondición o verificación posterior fallida aborta la transacción.
--   * No se restauran ni se conceden permisos a PUBLIC.
-- ============================================================================

BEGIN;

-- Firma exacta del objeto que se va a proteger. No continuar con una función
-- homónima, una sobrecarga distinta ni un objeto que no sea PROCEDURE.
DO $$
DECLARE
    v_procedimiento oid := to_regprocedure(
        'public.sp_cambiar_contrasena(integer,character varying,character varying)'
    );
    v_rol text;
BEGIN
    IF v_procedimiento IS NULL THEN
        RAISE EXCEPTION
            'No existe public.sp_cambiar_contrasena(integer, character varying, character varying)';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_proc
        WHERE oid = v_procedimiento
          AND prokind = 'p'
    ) THEN
        RAISE EXCEPTION
            'El objeto % existe pero no es un PROCEDURE', v_procedimiento::regprocedure;
    END IF;

    FOREACH v_rol IN ARRAY ARRAY[
        'anon',
        'authenticated',
        'service_role',
        'fashionstore_app',
        'fashionstore_provisioner'
    ]
    LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = v_rol) THEN
            RAISE EXCEPTION 'No existe el rol requerido: %', v_rol;
        END IF;
    END LOOP;
END $$;

-- Estado previo: ACL directa (incluye PUBLIC si aparece como grantee).
SELECT
    'ANTES_ACL_DIRECTA' AS verificacion,
    COALESCE(grantee.rolname, 'PUBLIC') AS grantee,
    acl.privilege_type,
    acl.is_grantable
FROM pg_proc AS procedimiento
CROSS JOIN LATERAL aclexplode(
    COALESCE(procedimiento.proacl, acldefault('f', procedimiento.proowner))
) AS acl
LEFT JOIN pg_roles AS grantee ON grantee.oid = acl.grantee
WHERE procedimiento.oid = to_regprocedure(
    'public.sp_cambiar_contrasena(integer,character varying,character varying)'
)
ORDER BY grantee, acl.privilege_type;

-- Estado previo: privilegios efectivos. has_function_privilege incorpora
-- PUBLIC y membresías heredables, no solo la ACL asignada directamente.
WITH roles_implicados(rolname) AS (
    VALUES ('anon'), ('authenticated'), ('service_role'),
           ('fashionstore_app'), ('fashionstore_provisioner')
)
SELECT
    'ANTES_EFECTIVO' AS verificacion,
    roles_implicados.rolname,
    roles.rolinherit,
    has_function_privilege(
        roles_implicados.rolname,
        to_regprocedure('public.sp_cambiar_contrasena(integer,character varying,character varying)'),
        'EXECUTE'
    ) AS puede_ejecutar
FROM roles_implicados
JOIN pg_roles AS roles USING (rolname)
ORDER BY roles_implicados.rolname;

-- Membresías que podrían explicar privilegios heredados en los roles revisados.
WITH roles_implicados(rolname) AS (
    VALUES ('anon'), ('authenticated'), ('service_role'),
           ('fashionstore_app'), ('fashionstore_provisioner')
)
SELECT
    'MEMBRESIA' AS verificacion,
    miembro.rolname AS miembro,
    padre.rolname AS miembro_de,
    membresia.admin_option
FROM pg_auth_members AS membresia
JOIN pg_roles AS miembro ON miembro.oid = membresia.member
JOIN pg_roles AS padre ON padre.oid = membresia.roleid
JOIN roles_implicados ON roles_implicados.rolname = miembro.rolname
ORDER BY miembro, miembro_de;

-- Revocación explícita. No se silencian errores: cualquier fallo aborta la
-- transacción completa y evita un estado parcial.
REVOKE EXECUTE ON PROCEDURE public.sp_cambiar_contrasena(
    integer, character varying, character varying
) FROM PUBLIC, anon, authenticated, service_role, fashionstore_app;

-- El rol de aprovisionamiento es el único rol previsto que conserva acceso.
GRANT EXECUTE ON PROCEDURE public.sp_cambiar_contrasena(
    integer, character varying, character varying
) TO fashionstore_provisioner;

-- Estado posterior: ACL directa.
SELECT
    'DESPUES_ACL_DIRECTA' AS verificacion,
    COALESCE(grantee.rolname, 'PUBLIC') AS grantee,
    acl.privilege_type,
    acl.is_grantable
FROM pg_proc AS procedimiento
CROSS JOIN LATERAL aclexplode(
    COALESCE(procedimiento.proacl, acldefault('f', procedimiento.proowner))
) AS acl
LEFT JOIN pg_roles AS grantee ON grantee.oid = acl.grantee
WHERE procedimiento.oid = to_regprocedure(
    'public.sp_cambiar_contrasena(integer,character varying,character varying)'
)
ORDER BY grantee, acl.privilege_type;

-- Estado posterior: privilegios efectivos, incluido acceso obtenido mediante
-- una membresía de rol. Debe fallar antes de COMMIT ante cualquier acceso no
-- autorizado o ante la pérdida de acceso de fashionstore_provisioner.
WITH roles_implicados(rolname) AS (
    VALUES ('anon'), ('authenticated'), ('service_role'),
           ('fashionstore_app'), ('fashionstore_provisioner')
)
SELECT
    'DESPUES_EFECTIVO' AS verificacion,
    roles_implicados.rolname,
    roles.rolinherit,
    has_function_privilege(
        roles_implicados.rolname,
        to_regprocedure('public.sp_cambiar_contrasena(integer,character varying,character varying)'),
        'EXECUTE'
    ) AS puede_ejecutar
FROM roles_implicados
JOIN pg_roles AS roles USING (rolname)
ORDER BY roles_implicados.rolname;

DO $$
DECLARE
    v_procedimiento oid := to_regprocedure(
        'public.sp_cambiar_contrasena(integer,character varying,character varying)'
    );
    v_rol text;
BEGIN
    FOREACH v_rol IN ARRAY ARRAY[
        'anon', 'authenticated', 'service_role', 'fashionstore_app'
    ]
    LOOP
        IF has_function_privilege(v_rol, v_procedimiento, 'EXECUTE') THEN
            RAISE EXCEPTION
                'Verificación fallida: % conserva EXECUTE efectivo sobre %',
                v_rol, v_procedimiento::regprocedure;
        END IF;
    END LOOP;

    IF NOT has_function_privilege(
        'fashionstore_provisioner', v_procedimiento, 'EXECUTE'
    ) THEN
        RAISE EXCEPTION
            'Verificación fallida: fashionstore_provisioner no conserva EXECUTE sobre %',
            v_procedimiento::regprocedure;
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_proc AS procedimiento
        CROSS JOIN LATERAL aclexplode(
            COALESCE(procedimiento.proacl, acldefault('f', procedimiento.proowner))
        ) AS acl
        WHERE procedimiento.oid = v_procedimiento
          AND acl.grantee = 0
          AND acl.privilege_type = 'EXECUTE'
    ) THEN
        RAISE EXCEPTION
            'Verificación fallida: PUBLIC conserva EXECUTE directo sobre %',
            v_procedimiento::regprocedure;
    END IF;
END $$;

COMMIT;

-- Recuperación segura ante un incidente posterior:
--   1. Investigar y corregir la causa (por ejemplo, un despliegue antiguo).
--   2. Si es imprescindible, conceder temporalmente EXECUTE SOLO al rol
--      fashionstore_app dentro de una transacción supervisada.
--   3. Repetir las verificaciones efectivas y retirar ese acceso al terminar.
-- Nunca conceder EXECUTE a PUBLIC, anon, authenticated ni service_role.
