-- PROPUESTA: no ejecutar sin aprobación expresa del propietario de la BD.
-- Ejecutar completa y una sola vez, después de respaldar la base y revisar los
-- privilegios de la aplicación. Inicializa cada suscripción con la configuración
-- que actualmente tiene su plan; no cambia sus datos comerciales.
BEGIN;
LOCK TABLE public.plan, public.plan_funcionalidad, public.suscripcion IN SHARE ROW EXCLUSIVE MODE;

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

INSERT INTO public.plan_version (idplan, numero, nombre, maxsucursales, estado, vigente)
SELECT idplan, 1, nombre, maxsucursales, estado, true FROM public.plan;
INSERT INTO public.plan_version_funcionalidad (idversion, codigo)
SELECT v.idversion, f.codigo FROM public.plan_version v
JOIN public.plan_funcionalidad f ON f.idplan = v.idplan WHERE v.numero = 1;
INSERT INTO public.suscripcion_plan_version (idsusc, idplan, idversion)
SELECT s.idsusc, s.idplan, v.idversion FROM public.suscripcion s
JOIN public.plan_version v ON v.idplan = s.idplan AND v.numero = 1;

ALTER TABLE public.plan_version ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.plan_version_funcionalidad ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.suscripcion_plan_version ENABLE ROW LEVEL SECURITY;
COMMIT;
