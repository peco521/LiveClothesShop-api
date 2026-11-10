-- PROPUESTA ANTERIOR de versionado individual: no corresponde al esquema real
-- ni al comportamiento actual de planes compartidos. No ejecutar para este módulo.
-- Migración del esquema SaaS NUEVO; no es migración de la instalación legacy.
-- Ejecutar una sola vez después de 03 y antes de 04_FashionStore_SaaS_ROLES.sql.
-- Crea una versión inicial equivalente a cada plan y asigna todas las
-- suscripciones existentes sin cambiar sus datos comerciales.
BEGIN;
LOCK TABLE public.plan, public.plan_funcionalidad, public.suscripcion IN SHARE ROW EXCLUSIVE MODE;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fashionstore_owner') THEN
        CREATE ROLE fashionstore_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fashionstore_app') THEN
        CREATE ROLE fashionstore_app NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fashionstore_provisioner') THEN
        CREATE ROLE fashionstore_provisioner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
    END IF;
END;
$$;
GRANT USAGE, CREATE ON SCHEMA public TO fashionstore_owner;

CREATE TABLE public.plan_version (
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
CREATE UNIQUE INDEX uq_plan_version_vigente ON public.plan_version(idplan) WHERE vigente;

CREATE TABLE public.plan_version_funcionalidad (
    idversion integer NOT NULL REFERENCES public.plan_version(idversion) ON DELETE RESTRICT,
    codigo varchar(30) NOT NULL REFERENCES public.funcionalidad_saas(codigo) ON DELETE RESTRICT,
    PRIMARY KEY (idversion, codigo)
);

ALTER TABLE public.suscripcion ADD CONSTRAINT uq_suscripcion_plan_version UNIQUE (idsusc, idplan);
CREATE TABLE public.suscripcion_plan_version (
    idsusc integer PRIMARY KEY,
    idplan smallint NOT NULL,
    idversion integer NOT NULL,
    FOREIGN KEY (idsusc, idplan) REFERENCES public.suscripcion(idsusc, idplan)
        ON UPDATE CASCADE ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (idplan, idversion) REFERENCES public.plan_version(idplan, idversion)
        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED
);

ALTER TABLE public.plan_version OWNER TO fashionstore_owner;
ALTER TABLE public.plan_version_funcionalidad OWNER TO fashionstore_owner;
ALTER TABLE public.suscripcion_plan_version OWNER TO fashionstore_owner;
ALTER SEQUENCE public.plan_version_idversion_seq OWNER TO fashionstore_owner;
GRANT SELECT ON public.plan_version, public.plan_version_funcionalidad,
    public.suscripcion_plan_version TO fashionstore_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.plan_version,
    public.plan_version_funcionalidad, public.suscripcion_plan_version
    TO fashionstore_provisioner;
GRANT USAGE, SELECT ON SEQUENCE public.plan_version_idversion_seq TO fashionstore_provisioner;

ALTER TABLE public.plan_version ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.plan_version FORCE ROW LEVEL SECURITY;
ALTER TABLE public.plan_version_funcionalidad ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.plan_version_funcionalidad FORCE ROW LEVEL SECURITY;
ALTER TABLE public.suscripcion_plan_version ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.suscripcion_plan_version FORCE ROW LEVEL SECURITY;

CREATE POLICY lectura_catalogo_versiones ON public.plan_version
    FOR SELECT USING (true);
CREATE POLICY bloqueo_version_para_trigger ON public.plan_version
    FOR UPDATE TO fashionstore_owner USING (true)
    WITH CHECK (true);
CREATE POLICY lectura_funcionalidades_versiones ON public.plan_version_funcionalidad
    FOR SELECT USING (true);
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
CREATE POLICY asignacion_trigger_version ON public.suscripcion_plan_version
    TO fashionstore_owner USING (true)
    WITH CHECK (true);

INSERT INTO public.plan_version (idplan, numero, nombre, maxsucursales, estado, vigente)
SELECT idplan, 1, nombre, maxsucursales, estado, true FROM public.plan;
INSERT INTO public.plan_version_funcionalidad (idversion, codigo)
SELECT v.idversion, f.codigo FROM public.plan_version v
JOIN public.plan_funcionalidad f ON f.idplan = v.idplan WHERE v.numero = 1;
INSERT INTO public.suscripcion_plan_version (idsusc, idplan, idversion)
SELECT s.idsusc, s.idplan, v.idversion FROM public.suscripcion s
JOIN public.plan_version v ON v.idplan = s.idplan AND v.numero = 1;

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
    VALUES (NEW.idsusc, NEW.idplan, v_idversion);
    RETURN NEW;
END;
$$;
CREATE TRIGGER trg_asignar_version_suscripcion
AFTER INSERT ON public.suscripcion
FOR EACH ROW EXECUTE FUNCTION public.fn_asignar_version_suscripcion();

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

REVOKE ALL ON FUNCTION public.fn_asignar_version_suscripcion() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.fn_plataforma_uso_sucursales(integer) FROM PUBLIC;
ALTER FUNCTION public.fn_tiene_funcionalidad(integer, varchar) OWNER TO fashionstore_owner;
ALTER FUNCTION public.fn_max_sucursales(integer) OWNER TO fashionstore_owner;
ALTER FUNCTION public.fn_asignar_version_suscripcion() OWNER TO fashionstore_owner;
ALTER FUNCTION public.fn_plataforma_uso_sucursales(integer) OWNER TO fashionstore_owner;
ALTER PROCEDURE public.sp_cambiar_plan(integer, smallint, date, date) OWNER TO fashionstore_owner;

COMMIT;
