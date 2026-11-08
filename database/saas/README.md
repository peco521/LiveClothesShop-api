# FashionStore SaaS y sincronización offline

## Alcance y seguridad del entorno

Esta carpeta, `LiveClothesShop-api/database/saas/`, es la copia canónica
versionada de los scripts SaaS. El directorio histórico
`C:\SI2_Parcial1\database\saas` se conserva fuera de Git y no debe usarse para
nuevos cambios ni como origen de ejecución.

Estos scripts preparan un **esquema nuevo de pruebas** en PostgreSQL. No son una
migración de los datos de la instalación anterior y no deben ejecutarse contra
Supabase ni contra la base actualmente utilizada por el equipo. La verificación
de este trabajo se limitó al cluster local `127.0.0.1:55432`, base
`fashionstore_test`.

Los artefactos tienen propósitos distintos:

1. `01_FashionStore_SaaS_TABLAS.sql`: creación inicial del esquema SaaS vacío,
   claves tenant, tablas y catálogos. No copia datos existentes.
2. `02_FashionStore_SaaS_TRIGGERS_PA.sql`: funciones, procedimientos,
   restricciones, triggers y RLS del esquema nuevo.
3. `03_FashionStore_SaaS_Plataforma_Sync.sql`: parches DDL repetibles sobre el
   esquema SaaS, funciones de administración de plataforma y motor SQL de sync.
   No migra datos de la instalación anterior.
4. `LiveClothesShop-api/app/modules/saas_suscripciones/planes_funcionalidades/migrations/001_versiones_planes.sql`:
   crea versiones iniciales equivalentes a los planes, asigna cada suscripción
   existente y reemplaza las funciones de funcionalidades/límites por consultas
   a la versión efectiva. Es una migración del esquema SaaS nuevo; no migra la
   instalación legacy.
5. `04_FashionStore_SaaS_ROLES.sql`: roles/grants mínimos para pruebas; requiere
   contraseñas locales entregadas con variables psql, no incrustadas en el archivo.
6. `LiveClothesShop-api/scripts/seed_demo.py`: datos de demostración ficticios,
   repetibles; no es un migrador.
7. `05_verificacion_rls.sql`: verificación transaccional de RLS y restricciones;
   no es un script de instalación.
8. `06_sync_venta_auditoria.sql`: parche repetible que registra en `bitacora` la
   venta offline aceptada dentro de la misma transacción; valida el contrato
   SaaS y no transforma datos del esquema legacy.

## Orden reproducible

En Windows, desde `C:\SI2_Parcial1\LiveClothesShop-api`:

```powershell
powershell -ExecutionPolicy Bypass -File database\saas\00_preparar_postgres_pruebas.ps1
.venv/Scripts/python.exe scripts/seed_demo.py
& 'C:\Program Files\PostgreSQL\17\bin\psql.exe' -h 127.0.0.1 -p 55432 `
  -U postgres -w -d fashionstore_test -v ON_ERROR_STOP=1 `
  -f database\saas\05_verificacion_rls.sql
```

`00_preparar_postgres_pruebas.ps1` prepara la base vacía y aplica 01–03,
`001_versiones_planes.sql` y 04, en ese orden. Si ya existe el esquema, omite
01–03 y aplica la migración solo si aún no existe `plan_version`; nunca
reconstruye una base existente. `-Recreate` borra y recrea el cluster/base local
y **no** debe usarse si se quiere conservar esos datos de pruebas.

Después de actualizar rutinas existentes en una base local ya preparada,
aplique `06_sync_venta_auditoria.sql` explícitamente a `fashionstore_test` con
`psql -h 127.0.0.1 -p 55432 -U postgres -w -d fashionstore_test -v
ON_ERROR_STOP=1 -f database\saas\06_sync_venta_auditoria.sql`.

Se generan archivos de acceso locales ignorados por Git:
`C:\SI2_Parcial1\.local\db_roles.env`, `backend.env` y
`LiveClothesShop-api\.local\demo_credentials.json`. Nunca copiar sus valores a
commits, logs o documentación. El seed conserva identificadores existentes y
no restablece las contraseñas de usuarios ya creados; la salida local conserva
credenciales iniciales solo para la demostración.

## Roles de base de datos

- `fashionstore_owner`: propietario/DDL, `NOLOGIN`, `NOBYPASSRLS`.
- `fashionstore_provisioner`: conexión separada para `/api/saas` y
  aprovisionamiento; `BYPASSRLS`, no usar en rutas tenant.
- `fashionstore_app`: conexión HTTP empresarial; `NOSUPERUSER`,
  `NOBYPASSRLS`, sin propiedad de tablas. RLS filtra filas por `app.idemp`,
  aplicado localmente a cada transacción.

La autenticación de plataforma (`tipo='S'`, `idEmp NULL`) y tenant están
separadas en la API. Los tokens de ambas categorías se rechazan mutuamente. El
endpoint de credenciales iniciales requiere `DEMO_MODE` y `SAAS-DEMO`; no
recupera claves de usuarios preexistentes.

## Sincronización

Endpoint receptor: `POST /api/sync/lote`; historial:
`GET /api/sync/operaciones`; resolución auditada:
`POST /api/sync/conflictos/{uuid}/resolver`. El lote tiene `dispositivo`,
`origen` y `operaciones[]`, cada una con UUID estable, `tipo` (`venta`/`reserva`),
`version` (1), `fecha`, `sucursal`, `datos` y `dependeDe[]`. Tenant e identidad
se derivan de la sesión, no del cuerpo. La respuesta contiene un resultado por
operación.

La clave persistente es `(idEmp,idOperacion)` y una huella SHA-256 canónica
rechaza reutilizar un UUID con payload diferente. Idempotencia y efecto
comercial se confirman en la misma transacción PostgreSQL. Cada operación tiene
su transacción, así el lote puede quedar parcialmente aceptado. Estados del
servidor: `confirmada`, `en_proceso`, `rechazada` y `conflicto`; clientes deben
presentar pendiente, reintento, sincronizada, conflicto o rechazada. Las
operaciones `en_proceso` antiguas se recuperan tras cinco minutos.

Contrato de identificadores: el UUID creado por el cliente offline es el mismo
UUID enviado en cada reintento; el servidor lo persiste tanto en
`operacion_sync.idOperacion` como en `venta.idOperacion`, ambos acotados por
`idEmp`. `sp_sync_confirmar` cierra el ledger, vincula el `nroVenta` y crea la
auditoría en la misma transacción de la venta/pago/inventario. No se genera un
segundo UUID. `venta.claveoperacion` pertenece al esquema legacy y no es alias
ni conversión implícita de `idOperacion`; ese campo se conserva en instalaciones
legacy. El camino de sync SaaS no escribe ni lee esa columna. La futura
migración de datos debe definir y auditar una correspondencia explícita antes
de copiar valores legacy; este paquete no migra históricos.

Conciliación:

- Venta: valida CU24, empresa/sucursal, permisos, catálogo, precio y stock; el
  procedimiento actualiza inventario. Efectivo puede aprobarse al sincronizar;
  tarjeta, QR y transferencia quedan pendientes de pasarela.
- Reserva: valida CU11 y las condiciones vigentes.
- Precio cambiado, stock insuficiente y reserva vencida son conflictos; datos,
  permisos, referencias y tipos inválidos son rechazos. Una dependencia
  rechazada causa conflicto dependiente. No se guardan datos de tarjeta.

El backend de sync está probado con PostgreSQL real en el cluster local. Los
clientes web/móvil tienen una base de cola, pero el worker Flutter en segundo
plano y el flujo completo offline de venta aún no están validados. El Service
Worker web solicita al cliente abierto que reintente; no garantiza ejecución de
segundo plano en todos los navegadores.

## Migración de la instalación anterior

No se ha escrito ni ejecutado un migrador de datos. Antes de una transición se
requiere inventario y copia de seguridad del origen, mapeo aprobado de empresa
para cada registro, desambiguación de IDs/correos/roles, traslado de secuencias y
validación de FKs. El esquema viejo usa `claveoperacion`; el nuevo usa UUID
`idOperacion`. No retirar ni alterar la base anterior hasta reconciliar conteos,
relaciones y flujos; mantener exportación verificable y procedimiento de retorno.

## Pruebas de PostgreSQL

```powershell
cd LiveClothesShop-api
.venv/Scripts/python.exe -m pytest tests/test_saas_postgres.py -q
```

Requiere `../.local/backend.env` (desde la API), PostgreSQL de pruebas y seed.
SQLite no sustituye RLS. La suite incluye concurrencia del límite de sucursales,
versiones de planes, CU07 administrativo, CU08 empresarial y CU10 catálogo; sus conteos son un
subconjunto de la suite API completa y no se suman otra vez.

Para reutilizar la base autorizada existente, no ejecutar creación ni seed como
paso de limpieza. La consulta CU08 no requiere nueva migración: utiliza `bitacora.idemp`,
sus FKs/RLS e índice existentes. Ver contrato y rutas en [`../../README.md`](../../README.md).

CU10 tampoco agrega DDL ni procedimientos: el SQL SaaS 01 ya define `idemp` y FKs
compuestas para productos, variantes, inventario y todos sus catálogos de filtro,
incluidos talla/color. El backend mapea esas columnas y aplica criterios tenant
además de RLS; no transforma los catálogos legacy globales ni requiere reinstalar
la base para habilitar la consulta autenticada. Las referencias canónicas y la
migración única de versiones de planes conservan el orden de instalación anterior.
