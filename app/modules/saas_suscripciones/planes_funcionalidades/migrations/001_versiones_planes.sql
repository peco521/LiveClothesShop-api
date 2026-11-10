-- Migración del esquema SaaS: versiones de planes y asignación de suscripciones.
-- Ejecutar una sola vez después de 03 y antes de 04_FashionStore_SaaS_ROLES.sql.
-- No crea ni altera roles de base de datos (eso corresponde a 04_roles_bootstrap_template.sql).
-- No modifica datos comerciales existentes de ninguna empresa.
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
    RAISE EXCEPTION 'Destino inesperado; se requiere target_db explícito';
  END IF;
END $$;

-- Verificar tablas base requeridas antes de tocar cualquier objeto.
DO $$
BEGIN
  IF to_regclass('public.plan') IS NULL THEN RAISE EXCEPTION 'Falta public.plan'; END IF;
  IF to_regclass('public.plan_funcionalidad') IS NULL THEN RAISE EXCEPTION 'Falta public.plan_funcionalidad'; END IF;
  IF to_regclass('public.suscripcion') IS NULL THEN RAISE EXCEPTION 'Falta public.suscripcion'; END IF;
  IF to_regclass('public.funcionalidad_saas') IS NULL THEN RAISE EXCEPTION 'Falta public.funcionalidad_saas'; END IF;
  IF to_regprocedure('public.fn_idemp_actual()') IS NULL THEN RAISE EXCEPTION 'Falta public.fn_idemp_actual()'; END IF;
END $$;

-- Verificar que no existan suscripciones duplicadas para el mismo (idsusc, idplan).
DO $$
DECLARE v_dup integer;
BEGIN
  SELECT count(*) INTO v_dup
  FROM (
    SELECT idsusc, idplan, count(*) AS c
    FROM public.suscripcion
    GROUP BY idsusc, idplan
    HAVING count(*) > 1
  ) d;
  IF v_dup > 0 THEN
    RAISE EXCEPTION 'Existen % suscripciones duplicadas para el mismo (idsusc, idplan); resolver antes de migrar', v_dup;
  END IF;
END $$;

-- Verificar que no existan suscripciones con plan inexistente.
DO $$
DECLARE v_inv integer;
BEGIN
  SELECT count(*) INTO v_inv
  FROM public.suscripcion s
  LEFT JOIN public.plan p ON p.idplan = s.idplan
  WHERE p.idplan IS NULL;
  IF v_inv > 0 THEN
    RAISE EXCEPTION 'Existen % suscripciones con plan inexistente; resolver antes de migrar', v_inv;
  END IF;
END $$;

-- Crear tabla plan_version de forma idempotente.
CREATE TABLE IF NOT EXISTS public.plan_version (
    idversion integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    idplan smallint NOT NULL REFERENCES public.plan(idplan) ON DELETE RESTRICT,
    numero integer NOT NULL CHECK (numero > 0),
    nombre varchar(30) NOT NULL,
    maxsucursales smallint NOT NULL CHECK (maxsucursales > 0),
    estado varchar(20) NOT NULL CHECK (estado IN ('activo', 'inactivo')),
    vigente boolean NOT NULL DEFAULT false,
    creadaen timestamptz NOT NULL DEFAULT now(),
    UNIQUE (idplan, numero),
    UNIQUE (idplan, idversion)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_plan_version_vigente ON public.plan_version(idplan) WHERE vigente;

-- Crear tabla plan_version_funcionalidad de forma idempotente.
CREATE TABLE IF NOT EXISTS public.plan_version_funcionalidad (
    idversion integer NOT NULL REFERENCES public.plan_version(idversion) ON DELETE RESTRICT,
    codigo varchar(30) NOT NULL REFERENCES public.funcionalidad_saas(codigo) ON DELETE RESTRICT,
    PRIMARY KEY (idversion, codigo)
);

-- Constraint único en suscripcion solo si no existen duplicados (ya verificados arriba).
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'public.suscripcion'::regclass
      AND conname = 'uq_suscripcion_plan_version'
  ) THEN
    ALTER TABLE public.suscripcion
      ADD CONSTRAINT uq_suscripcion_plan_version UNIQUE (idsusc, idplan);
  END IF;
END $$;

-- Crear tabla suscripcion_plan_version de forma idempotente.
CREATE TABLE IF NOT EXISTS public.suscripcion_plan_version (
    idsusc integer PRIMARY KEY,
    idplan smallint NOT NULL,
    idversion integer NOT NULL,
    FOREIGN KEY (idsusc, idplan) REFERENCES public.suscripcion(idsusc, idplan)
        ON UPDATE CASCADE ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (idplan, idversion) REFERENCES public.plan_version(idplan, idversion)
        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED
);

-- Nota Supabase: no se puede ALTER ... OWNER TO fashionstore_owner
-- sin ser superusuario. Las tablas quedan owned por el creador.
-- fashionstore_owner es NOLOGIN y no necesita ser propietario de las tablas.

-- Grants de lectura a fashionstore_app (escritura se concede en 07_access_grants_incremental.sql).
GRANT SELECT ON public.plan_version, public.plan_version_funcionalidad,
    public.suscripcion_plan_version TO fashionstore_app;
GRANT USAGE, SELECT ON SEQUENCE public.plan_version_idversion_seq TO fashionstore_provisioner;

-- RLS en las nuevas tablas (solo si fashionstore_app existe y es NOBYPASSRLS).
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles
                 WHERE rolname='fashionstore_app' AND NOT rolsuper AND NOT rolbypassrls) THEN
    RAISE EXCEPTION 'fashionstore_app debe existir y ser NOBYPASSRLS antes de activar RLS';
  END IF;
END $$;

ALTER TABLE public.plan_version ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.plan_version FORCE ROW LEVEL SECURITY;
ALTER TABLE public.plan_version_funcionalidad ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.plan_version_funcionalidad FORCE ROW LEVEL SECURITY;
ALTER TABLE public.suscripcion_plan_version ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.suscripcion_plan_version FORCE ROW LEVEL SECURITY;

-- Políticas RLS idempotentes (crear solo si no existen).
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_policies
                 WHERE schemaname='public' AND tablename='plan_version'
                   AND policyname='lectura_catalogo_versiones') THEN
    CREATE POLICY lectura_catalogo_versiones ON public.plan_version
        FOR SELECT USING (true);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_policies
                 WHERE schemaname='public' AND tablename='plan_version'
                   AND policyname='bloqueo_version_para_trigger') THEN
    CREATE POLICY bloqueo_version_para_trigger ON public.plan_version
        FOR UPDATE TO fashionstore_owner USING (true) WITH CHECK (true);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_policies
                 WHERE schemaname='public' AND tablename='plan_version_funcionalidad'
                   AND policyname='lectura_funcionalidades_versiones') THEN
    CREATE POLICY lectura_funcionalidades_versiones ON public.plan_version_funcionalidad
        FOR SELECT USING (true);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_policies
                 WHERE schemaname='public' AND tablename='suscripcion_plan_version'
                   AND policyname='tenant_suscripcion_version') THEN
    CREATE POLICY tenant_suscripcion_version ON public.suscripcion_plan_version
        TO fashionstore_app
        USING (EXISTS (
            SELECT 1 FROM public.suscripcion s
            WHERE s.idsusc = suscripcion_plan_version.idsusc
              AND s.idemp = public.fn_idemp_actual()
        ))
        WITH CHECK (EXISTS (
            SELECT 1 FROM public.suscripcion s
            WHERE s.idsusc = suscripcion_plan_version.idsusc
              AND s.idemp = public.fn_idemp_actual()
        ));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_policies
                 WHERE schemaname='public' AND tablename='suscripcion_plan_version'
                   AND policyname='asignacion_trigger_version') THEN
    CREATE POLICY asignacion_trigger_version ON public.suscripcion_plan_version
        TO fashionstore_owner USING (true) WITH CHECK (true);
  END IF;
END $$;

-- Insertar versiones iniciales solo si aún no existen (idempotente).
INSERT INTO public.plan_version (idplan, numero, nombre, maxsucursales, estado, vigente)
SELECT p.idplan, 1, p.nombre, p.maxsucursales, p.estado, true
FROM public.plan p
WHERE NOT EXISTS (
    SELECT 1 FROM public.plan_version v WHERE v.idplan = p.idplan AND v.numero = 1
);

INSERT INTO public.plan_version_funcionalidad (idversion, codigo)
SELECT v.idversion, f.codigo
FROM public.plan_version v
JOIN public.plan_funcionalidad f ON f.idplan = v.idplan
WHERE v.numero = 1
  AND NOT EXISTS (
    SELECT 1 FROM public.plan_version_funcionalidad vf
    WHERE vf.idversion = v.idversion AND vf.codigo = f.codigo
  );

INSERT INTO public.suscripcion_plan_version (idsusc, idplan, idversion)
SELECT s.idsusc, s.idplan, v.idversion
FROM public.suscripcion s
JOIN public.plan_version v ON v.idplan = s.idplan AND v.numero = 1
WHERE NOT EXISTS (
    SELECT 1 FROM public.suscripcion_plan_version sv WHERE sv.idsusc = s.idsusc
);

-- Funciones y procedimiento (CREATE OR REPLACE para idempotencia).
CREATE OR REPLACE FUNCTION public.fn_tiene_funcionalidad(p_idEmp integer, p_codigo varchar)
RETURNS boolean
LANGUAGE sql STABLE
AS $$
    SELECT public.fn_suscripcion_vigente(p_idEmp) AND EXISTS (
        SELECT 1
        FROM public.suscripcion s
        JOIN public.suscripcion_plan_version sv ON sv.idsusc = s.idsusc AND sv.idplan = s.idplan
        JOIN public.plan_version v ON v.idversion = sv.idversion AND v.idplan = s.idplan
        JOIN public.plan_version_funcionalidad vf ON vf.idversion = v.idversion
        WHERE s.idemp = p_idEmp AND s.estado = 'activa'
          AND current_date BETWEEN s.fechainicio AND s.fechavencimiento
          AND v.estado = 'activo' AND vf.codigo = p_codigo
    );
$$;

CREATE OR REPLACE FUNCTION public.fn_max_sucursales(p_idEmp integer)
RETURNS integer
LANGUAGE plpgsql STABLE
AS $$
DECLARE
    v_max integer;
BEGIN
    SELECT v.maxsucursales INTO v_max
    FROM public.suscripcion s
    JOIN public.suscripcion_plan_version sv ON sv.idsusc = s.idsusc AND sv.idplan = s.idplan
    JOIN public.plan_version v ON v.idversion = sv.idversion AND v.idplan = s.idplan
    WHERE s.idemp = p_idEmp AND s.estado = 'activa'
      AND current_date BETWEEN s.fechainicio AND s.fechavencimiento
      AND v.estado = 'activo';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'No existe una versión activa válida para la suscripción de la empresa %', p_idEmp;
    END IF;
    RETURN v_max;
END;
$$;

CREATE OR REPLACE FUNCTION public.fn_asignar_version_suscripcion()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_idversion integer;
BEGIN
    SELECT idversion INTO v_idversion
    FROM public.plan_version
    WHERE idplan = NEW.idplan AND vigente AND estado = 'activo'
    FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'El plan % no tiene una versión vigente activa', NEW.idplan;
    END IF;
    INSERT INTO public.suscripcion_plan_version(idsusc, idplan, idversion)
    VALUES (NEW.idsusc, NEW.idplan, v_idversion)
    ON CONFLICT (idsusc) DO UPDATE SET idplan = EXCLUDED.idplan, idversion = EXCLUDED.idversion;
    RETURN NEW;
END;
$$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_asignar_version_suscripcion') THEN
    CREATE TRIGGER trg_asignar_version_suscripcion
    AFTER INSERT ON public.suscripcion
    FOR EACH ROW EXECUTE FUNCTION public.fn_asignar_version_suscripcion();
  END IF;
END $$;

CREATE OR REPLACE FUNCTION public.fn_plataforma_uso_sucursales(p_idEmp integer)
RETURNS TABLE(usadas integer, limite integer)
LANGUAGE plpgsql STABLE
AS $$
DECLARE
    v_idsusc integer;
    v_limite integer;
BEGIN
    SELECT s.idsusc, v.maxsucursales INTO v_idsusc, v_limite
    FROM public.suscripcion s
    LEFT JOIN public.suscripcion_plan_version sv
      ON sv.idsusc = s.idsusc AND sv.idplan = s.idplan
    LEFT JOIN public.plan_version v
      ON v.idversion = sv.idversion AND v.idplan = s.idplan
    WHERE s.idemp = p_idEmp AND s.estado = 'activa'
      AND current_date BETWEEN s.fechainicio AND s.fechavencimiento;
    IF v_idsusc IS NOT NULL AND v_limite IS NULL THEN
        RAISE EXCEPTION 'La suscripción activa de la empresa % no tiene una versión válida', p_idEmp;
    END IF;
    RETURN QUERY
    SELECT (SELECT count(*)::integer FROM public.sucursal s
            WHERE s.idemp = p_idEmp AND s.estado = 'activo'),
           COALESCE(v_limite, 0);
END;
$$;

CREATE OR REPLACE PROCEDURE public.sp_cambiar_plan(
    p_idEmp integer, p_idPlan smallint, p_fechaInicio date, p_fechaVencimiento date
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_max integer;
    v_act integer;
BEGIN
    IF p_fechaVencimiento < p_fechaInicio THEN
        RAISE EXCEPTION 'La fecha de vencimiento debe ser igual o posterior al inicio';
    END IF;
    SELECT maxsucursales INTO v_max
    FROM public.plan_version
    WHERE idplan = p_idPlan AND vigente AND estado = 'activo'
    FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'El plan % no tiene una versión vigente activa', p_idPlan;
    END IF;
    PERFORM pg_advisory_xact_lock(1001, p_idEmp);
    IF NOT EXISTS (SELECT 1 FROM public.empresa WHERE idemp = p_idEmp) THEN
        RAISE EXCEPTION 'La empresa % no existe', p_idEmp;
    END IF;
    SELECT count(*) INTO v_act FROM public.sucursal
    WHERE idemp = p_idEmp AND estado = 'activo';
    IF v_act > v_max THEN
        RAISE EXCEPTION 'La empresa tiene % sucursales activas y la versión solicitada solo permite %', v_act, v_max;
    END IF;
    UPDATE public.suscripcion SET estado = 'reemplazada'
    WHERE idemp = p_idEmp AND estado = 'activa';
    INSERT INTO public.suscripcion (idemp, idplan, fechainicio, fechavencimiento)
    VALUES (p_idEmp, p_idPlan, p_fechaInicio, p_fechaVencimiento);
END;
$$;

-- Nota Supabase: no se puede ALTER FUNCTION/PROCEDURE OWNER TO fashionstore_owner
-- sin ser superusuario. Las funciones quedan owned por el creador.
-- fashionstore_owner es NOLOGIN y no necesita ser propietario.

-- Revocar acceso público a funciones administrativas.
REVOKE ALL ON FUNCTION public.fn_asignar_version_suscripcion() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.fn_plataforma_uso_sucursales(integer) FROM PUBLIC;

COMMIT;
