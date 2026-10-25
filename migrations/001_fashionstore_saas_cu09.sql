-- Aplicar SOLO en una base nueva, después de database/saas/01_...TABLAS.sql
-- y antes de database/saas/02_...TRIGGERS_PA.sql. No aplicar sobre la BD actual.
-- Ejecutar con el rol de aprovisionamiento (propietario privilegiado), nunca
-- con el rol de peticiones. El esquema SaaS original no incluye estas columnas.
BEGIN;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
ALTER TABLE rol ADD COLUMN estado varchar(15) NOT NULL DEFAULT 'activo'
    CHECK (estado IN ('activo','inactivo'));
ALTER TABLE empleado ADD COLUMN estado varchar(15) NOT NULL DEFAULT 'activo'
    CHECK (estado IN ('activo','inactivo'));
ALTER TABLE sucursal ADD COLUMN latitud numeric(9,6), ADD COLUMN longitud numeric(9,6);
ALTER TABLE detalleventa ADD COLUMN descuentounitario numeric(10,2)
    CHECK (descuentounitario >= 0);
-- CU07 admite nombres NULL preexistentes; la validación de altas se conserva
-- en la API. El borrador SaaS los declara NOT NULL inicialmente.
ALTER TABLE usuario ALTER COLUMN nombre DROP NOT NULL;

-- El contrato HTTP legado claveOperacion se conserva. El esquema SaaS
-- guarda idOperacion UUID y UNIQUE(idEmp,idOperacion), pero la API conserva
-- claveoperacion en el modo antiguo y cierra caja SaaS. Antes de habilitarla,
-- adaptar todos los consumidores y verificar idempotencia por tenant. Para
-- importar una venta de la BD antigua, convertir
-- claveoperacion::uuid con revisión previa de valores no UUID y duplicados.
-- No eliminar claveoperacion de la BD antigua antes de migrar sus datos y
-- todos sus consumidores. La ruta de caja queda cerrada hasta esa integración.

-- Única lectura anterior a SET LOCAL: código exacto, activo y vigente.
-- El dueño de esta función debe ser el rol de aprovisionamiento con BYPASSRLS;
-- no conceder propiedad, BYPASSRLS ni SELECT global al rol HTTP.
CREATE FUNCTION auth_resolver_empresa(p_codigo varchar) RETURNS integer
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT e.idemp FROM public.empresa e
  WHERE e.codigo = p_codigo AND e.estado = 'activa'
    AND EXISTS (SELECT 1 FROM public.suscripcion s
                WHERE s.idemp = e.idemp AND s.estado = 'activa'
                  AND current_date BETWEEN s.fechainicio AND s.fechavencimiento)
$$;
REVOKE ALL ON FUNCTION auth_resolver_empresa(varchar) FROM PUBLIC;
-- El administrador otorga EXECUTE sobre esta función exclusivamente al rol
-- de peticiones; SELECT/INSERT/UPDATE mínimos sobre tablas tenant se otorgan
-- también a ese rol, que debe tener NOBYPASSRLS y no ser propietario.
COMMIT;
