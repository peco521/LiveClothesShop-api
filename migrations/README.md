# FashionStore: integración inicial por empresa

La base actual y Supabase no se modifican. El SQL en esta carpeta se prepara
para una **base PostgreSQL nueva y separada**. `001_fashionstore_saas_cu09.sql`
complementa `../../database/saas/01_FashionStore_SaaS_TABLAS.sql` (ubicado en el
directorio padre del repositorio API); después corresponde aplicar
`02_FashionStore_SaaS_TRIGGERS_PA.sql`, que instala el límite del plan, FK
compuestas y políticas RLS. Esos dos borradores externos aún necesitan revisión
de integración antes de desplegar; no son migraciones sobre la BD existente.

## Efecto exacto de `001_fashionstore_saas_cu09.sql`

Es un parche **DDL para el borrador SaaS recién creado**, dentro de una sola
transacción, no un conversor del esquema antiguo:

1. Revoca `CREATE` público sobre el esquema `public`; no concede permisos al
   usuario HTTP.
2. Añade `rol.estado` y `empleado.estado` (`varchar(15) NOT NULL`, valor
   predeterminado `activo`, CHECK para `activo`/`inactivo`). Si ya hubiera filas
   en esas tablas del esquema SaaS, PostgreSQL les asignaría `activo`; no lee ni
   copia el estado de otra base.
3. Añade `sucursal.latitud` y `sucursal.longitud` (`numeric(9,6)`, nulos por
   defecto) y `detalleventa.descuentounitario` (`numeric(10,2)`, nullable, CHECK
   no negativo). No calcula coordenadas ni descuentos para ventas anteriores.
4. Admite `usuario.nombre` nulo para conservar las cuentas heredadas cuyo
   antiguo campo `nombres` era nullable. No copia ni renombra `nombres`.
5. Crea `auth_resolver_empresa(codigo)` como función `SECURITY DEFINER`: solo
   devuelve `idEmp` de empresas activas con suscripción activa y vigente;
   restringe el `search_path` y revoca su `EXECUTE` a `PUBLIC`. Su propietario
   debe poder consultar las tablas bajo `FORCE ROW LEVEL SECURITY`; la
   concesión específica de `EXECUTE` al rol HTTP queda pendiente.

El script **no crea** `empresa`, `plan`, `suscripcion`, `idEmp`, FKs compuestas,
RLS, índices de venta ni límites de sucursal: eso proviene del borrador 01 y
del script 02. Tampoco crea roles PostgreSQL ni otorga DML. No es idempotente:
solo se ejecutaría una vez sobre el esquema nuevo y vacío en el orden indicado.

## Datos anteriores todavía no migrados

No hay ninguna instrucción `INSERT`/`UPDATE` que importe datos de la BD
anterior: faltan asignación verificable de empresa a cada registro (incluidos
usuarios, roles, ciudades, sucursales, horarios y auditoría), provisión de
planes/suscripciones, normalización y desambiguación de correos duplicados,
traslado de nombres heredados y preservación de IDs, FKs y secuencias. El
esquema anterior tampoco aporta automáticamente las nuevas columnas de estado,
coordenadas y descuentos si están ausentes en esa instalación; se requiere un
inventario previo por entorno. Nunca aplicar este DDL directamente a la BD
actual.

El rol HTTP PostgreSQL debe ser distinto del propietario, `NOBYPASSRLS`, sin
permisos de aprovisionamiento ni de cron. El propietario privilegiado instala
el esquema y la función `auth_resolver_empresa`, y concede `EXECUTE` de esta
única función al rol HTTP. El rol HTTP requiere DML únicamente sobre las tablas
tenant necesarias y lectura de las tablas globales del plan; la resolución
anónima nunca acepta `idEmp`. La sesión autenticada restaura `app.idemp`
con `set_config(..., true)` en cada transacción. No usar una conexión de
administración para servir peticiones.

`claveOperacion` continúa como campo HTTP de caja y el modo anterior conserva
`venta.claveoperacion` y su unicidad global. El destino del esquema SaaS es
`venta.idOperacion UUID`, único por `(idEmp,idOperacion)`. Antes de migrar datos
de una instalación anterior, revisar valores no UUID y duplicados de
`claveoperacion`, convertirlos y mantener copia/compatibilidad hasta migrar
todos los consumidores. **El script 001 no añade, convierte ni rellena
`idOperacion` a partir de `claveoperacion`, ni elimina esta última**: el
`idOperacion UUID` y su unicidad por empresa ya aparecen en el esquema SaaS
nuevo, no en el parche. Tampoco mueve pagos ni detalles de venta. Los
consumidores legados de `claveoperacion` permanecen en el modo anterior: antes
de habilitar la caja SaaS se deberán adaptar juntos al nuevo UUID, comprobar
la idempotencia por `(idEmp,idOperacion)` y preservar las referencias previas.
La ruta de caja permanece deshabilitada en modo SaaS.

Rutas habilitadas inicialmente: `/api/auth/login` (también `/admin` y
`/cliente`), `/api/auth/me`, `/api/auth/logout`, `/api/admin/ciudades` y
`/api/admin/sucursales` con sus subrutas. Pendientes y cerradas con 503:
registro, recuperación, bootstrap, CU05–CU08, catálogo/consulta pública,
reservas, carrito, compras, pagos y webhook Stripe, historial,
recomendaciones, inventario, proveedores, promociones, caja, devoluciones,
reportes y vencimiento global de reservas. El aprovisionamiento y el cron
global requieren procesos y credenciales separados antes de habilitarlos.

La prueba SQLite `tests/test_multitenant.py` verifica filtros de aplicación,
tokens, ciudades, sucursales y horarios de dos empresas. **No verifica RLS**:
falta ejecutar sobre una base PostgreSQL de pruebas aislada pruebas de lectura
y escritura cruzada, referencias compuestas, límites de plan, revocación de
permisos y contexto después de commit/rollback con un rol sin BYPASSRLS.
