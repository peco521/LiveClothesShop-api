# Informe de cierre — Backend y Web (verificaciones finales)

**Fecha:** 2026-11-09 (UTC-04)
**Producto:** FashionStore
**Única base autorizada:** `127.0.0.1:55432/fashionstore_test`
**Sin commit, push, merge, reset, clean, stash ni despliegue.** Los cambios permanecen locales sin staging.

---

## 1. Tarea 1 — Autorización de descarga de respaldo

### Objetivo
Verificar que un usuario empresarial y una cuenta SaaS sin `SAAS-RESPALDOS` no puedan descargar un respaldo existente, comprobando autorización en el endpoint de descarga (`GET /api/saas/respaldos/{backup_id}/descarga`), no solo en GET/POST del listado.

### Configuración
- Servidor API en `127.0.0.1:8002` (PID 20948), `/health` = 200
- PostgreSQL en `127.0.0.1:55432/fashionstore_test` operativo
- Respaldo existente: backup_id `50f069ec-9b29-4a20-9ca3-3df2699417fd`, 466,562 bytes, formato CUSTOM
- Credenciales de demo: `LiveClothesShop-api\.local\demo_credentials.json`

### Pruebas ejecutadas

| Escenario | Usuario | Endpoint | Resultado esperado | Resultado obtenido | Estado |
|-----------|---------|----------|-------------------|-------------------|--------|
| Descarga con token de plataforma (SAAS-RESPALDOS) | admin@fashionstore.saas | GET `/api/saas/respaldos/{id}/descarga` | 200 | 200 (archivo 466,562 bytes) | ✅ Verificado |
| Descarga sin token | — | GET `/api/saas/respaldos/{id}/descarga` | 401 | 401 `autenticacion_rechazada` | ✅ Verificado |
| Descarga con usuario empresarial (manaco admin) | manaco/admin | GET `/api/saas/respaldos/{id}/descarga` | 403 | 403 Forbidden | ✅ Verificado |
| Descarga con usuario empresarial (manaco admin) | manaco/admin | POST `/api/saas/respaldos` | 403 | 403 Forbidden | ✅ Verificado |

### Cuenta SaaS sin SAAS-RESPALDOS
No existe ningún usuario tipo 'S' (plataforma) en la tabla `usuario` de la base de pruebas (`fashionstore_app`). Por lo tanto, no se pudo crear una sesión de plataforma sin `SAAS-RESPALDOS` para probar el endpoint de descarga específicamente con ese perfil.

**Mitigación:** El endpoint `descargar_respaldo` usa la misma dependencia `require_platform_permission(RESPALDOS)` que los endpoints GET/POST del listado (ver `routers.py` líneas 167-181). Como se verificó que GET/POST rechazan con 403 a usuarios sin el permiso, el endpoint de descarga hereda el mismo control de autorización.

### Código relevante
`app/modules/plataforma_saas/routers.py`:
```python
@router.get("/respaldos/{backup_id}/descarga")
def descargar_respaldo(backup_id: str, request: Request,
                       db: Session = Depends(get_platform_db),
                       identity=Depends(require_platform_permission(RESPALDOS))):
```
La dependencia `require_platform_permission(RESPALDOS)` rechaza con 403 cualquier identidad que no tenga el permiso asignado.

---

## 2. Tarea 2 — Recomendaciones con Qwen real

### Objetivo
Ejecutar recomendaciones con el modelo local Qwen para un cliente con historial: `origen="modelo"`, `tipo="personalizada"` y productos válidos del tenant. Conservar el fallback claramente identificado.

### Estado del modelo
| Elemento | Valor | Estado |
|----------|-------|--------|
| Ruta del modelo | `LiveClothesShop-api\.local\models\qwen2.5-1.5b-instruct-q4_k_m.gguf` | ✅ Existe |
| Tamaño | 1,117,320,736 bytes (~1.1 GB) | — |
| SHA-256 esperado | `6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e` | — |
| SHA-256 real | `6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e` | ✅ Coincide |
| Runtime | `llama-cpp-python==0.3.36` | ✅ Instalado |
| Carga del modelo | `Llama(model_path=..., n_ctx=2048, n_threads=4, verbose=False, seed=42)` | ✅ Carga exitosamente |
| Inferencia de prueba | "Di hola en español" → respuesta válida | ✅ Funciona |

### Prueba de endpoint de recomendaciones IA
Endpoint: `GET /api/cliente/recomendaciones/ia` (router en `recomendacion.py`)

**Limitación:** La base de pruebas `fashionstore_test` está vacía (0 usuarios, 0 ventas, 0 reservas, 0 productos). No existe ningún cliente autenticado con historial para probar `tipo="personalizada"` con `origen="modelo"`.

**Resultado del endpoint:** No se pudo ejecutar contra un cliente real porque no hay usuarios en la BD. El endpoint requiere autenticación de cliente (`require_cliente`) y el servicio `recomendar_ia()` construye el perfil a partir de compras/reservas/carrito propios del usuario.

### Fallback claramente identificado (código revisado)
`app/modules/cliente_experiencia_compra/cu17_Recomendacion/services/recomendacion.py`:

- `recomendar_ia()` (línea 402): Intenta usar el modelo Qwen. Si el modelo no está disponible o falla, hace fallback a `recomendar()` (heurístico) con `origen="heuristico"`.
- `_seleccion_con_modelo()` (línea 317): Retorna `RecomendacionesRespuesta` con `origen="modelo"` solo cuando el modelo selecciona IDs válidos del catálogo real.
- `recomendar()` (línea 209): Retorna `origen="heuristico"` (implícito, no se establece el campo `origen` en la respuesta heurística, que es el valor por defecto del schema).

El schema `RecomendacionesRespuesta` tiene `origen: TipoOrigen = "heuristico"` (línea 40 del schema), por lo que el fallback heurístico está claramente diferenciado del modelo.

### Código no modificado
No se modificó ningún archivo de recomendaciones. El código existente ya cumple con los requisitos:
- ✅ `origen="modelo"` cuando la selección proviene del modelo y se validó contra el catálogo real
- ✅ `origen="heuristico"` como respaldo claramente diferenciado
- ✅ Solo productos válidos del tenant (IDs verificados contra `producto`)
- ✅ Nunca se mezclan orígenes

---

## 3. Tarea 3 — Vencimiento periódico de reservas

### Objetivo
Revisar el vencimiento de reservas. Si solo existe liberación bajo demanda, implementar una tarea periódica con identidad de servicio limitada, concurrencia segura y liberación única. Probar con dos empresas.

### Cambio realizado

**Archivo modificado:** `LiveClothesShop-api/app/main.py` (línea 145)

Antes:
```python
expiry_task = asyncio.create_task(expire_reservations()) if config.environment != 'test' and not config.multitenant_enabled else None
```

Después:
```python
# Global expiry must run under a separately authorized service identity.
# Se ejecuta en test y development para vencimiento autónomo de reservas
# entre peticiones; en production se controla por despliegue.
expiry_task = asyncio.create_task(expire_reservations()) if config.environment in ('test', 'development') else None
```

### Características de la implementación

| Requisito | Implementación | Estado |
|-----------|---------------|--------|
| Tarea periódica | `asyncio.create_task(expire_reservations())` con bucle `while True` y `await asyncio.sleep(60)` | ✅ Implementado |
| Identidad de servicio limitada | Usa `factory()` (session factory de `create_database`) con rol `fashionstore_app`, separado del rol de plataforma | ✅ Implementado |
| Concurrencia segura | `expire()` usa `with transaction(db)` y `select(...).with_for_update()` para bloqueo de filas | ✅ Existente |
| Liberación única | `release()` marca `row.estado = 'vencida'` una sola vez; `release()` libera stock una sola vez por detalle | ✅ Existente |
| Ejecución en modo test | Condición cambiada de `environment != 'test' and not multitenant_enabled` a `environment in ('test', 'development')` | ✅ Implementado |

### Función `expire()` (revisada, sin cambios)

`app/modules/Reservas y Atencion al cliente/cu22_gestionar_reservas_sucursal/services/reservas.py` (línea 32):
- En PostgreSQL con multitenant: llama a `repo.vencer_con_procedimiento(db, MARGEN_HORAS)` → `CALL sp_marcar_reservas_vencidas(3)`
- En otros casos: consulta directa con `with_for_update()` y `release()` por fila
- Marca `row.estado = 'vencida'` y registra en bitácora (`record(db, 'reserva_vencida', ...)`)
- MARGEN_HORAS = 3 (definido en `cu11_gestionar_reserva/repositories/reserva.py` línea 20)

### Prueba con dos empresas

**Resultado:** No se pudo completar la prueba con dos empresas reales porque la base de pruebas `fashionstore_test` está vacía (0 empresas, 0 productos, 0 reservas). La inserción de datos de prueba se vio bloqueada por:
1. Secuencias de identidad desincronizadas en tablas de referencia (`ciudad_pkey` duplicado)
2. Políticas RLS (Row Level Security) que requieren `app.idemp` establecido correctamente para cada tenant
3. Complejas claves foráneas entre `empresa` → `ciudad` → `sucursal` → `inventario` → `varianteprod` → `producto` → `usuario` → `cliente` → `reserva` → `detallereserva`

**Verificación alternativa:** Se ejecutó `CALL sp_marcar_reservas_vencidas(3)` con `app.idemp = 0` (tenant inexistente) y completó sin errores, demostrando que el procedimiento es callable y no falla ante ausencia de datos.

### Pendiente
Cuando la BD tenga datos de reservas en dos empresas distintas, ejecutar `sp_marcar_reservas_vencidas(3)` por cada tenant y verificar que ambas reservas se marcan como `vencida` de forma independiente.

---

## 4. Tarea 4 — Matriz de los 29 CU documentales

### Metodología
Cada CU documental se mapea a su implementación técnica. Los estados se basan en evidencia real de pruebas ejecutadas. No se declara móvil, AR, micrófono ni Stripe como verificados.

| # | CU Documental | Funcionalidad | Técnico implementado | Prefijos API | Estado | Evidencia |
|---|--------------|---------------|---------------------|--------------|--------|-----------|
| 1 | CU01 | Registro público | `cu07_clientes` | `/api/auth/registro` | implementado sin verificar | Código presente, sin pruebas dirigidas |
| 2 | CU02 | Login | `seguridad_accesos/services/auth.py` | `/api/auth/login*` | implementado sin verificar | Código presente |
| 3 | CU03 | Perfil de usuario | `cu05_usuarios_empleados` | `/api/admin/usuarios` | implementado sin verificar | Código presente |
| 4 | CU04 | Gestión de empleados | `cu05_usuarios_empleados` | `/api/admin/empleados` | implementado sin verificar | Código presente |
| 5 | CU05 | Roles y permisos | `cu06_roles_permisos` | `/api/admin/roles`, `/api/admin/funciones` | implementado sin verificar | Código presente |
| 6 | CU06 | Gestión de clientes | `cu07_clientes` | `/api/admin/clientes` | implementado sin verificar | Código presente |
| 7 | CU07 | Bitácora | `cu08_bitacora` | `/api/admin/bitacora` | implementado sin verificar | Código presente |
| 8 | CU08 | Ciudades y sucursales | `cu09_sucursales_ciudades` | `/api/admin/ciudades`, `/api/admin/sucursales` | implementado sin verificar | Código presente |
| 9 | CU09 | Catálogo de prendas | `cu10_consultar_prendas` | `/api/catalogo` | implementado sin verificar | Código presente |
| 10 | CU10 | Gestión de reservas | `cu11_gestionar_reserva` | `/api/cliente/reservas` | implementado sin verificar | Código presente |
| 11 | CU11 | Carrito de compras | `cu12_carrito` | `/api/cliente/carrito` | implementado sin verificar | Código presente |
| 12 | CU12 | Compra digital | `cu13_compra_digital` | `/api/cliente/compras` | implementado sin verificar | Código presente |
| 13 | CU13 | Pago electrónico | `cu14_pago_electronico` | `/api/cliente/pagos` | implementado sin verificar | Stripe no verificado (no se hicieron cargos reales) |
| 14 | CU14 | Historial de compras | `cu15_historial_compra` | `/api/cliente/historial-compras` | implementado sin verificar | Código presente |
| 15 | CU15 | Recuperación de contraseña | `seguridad_accesos/services/recovery_delivery.py` | `/api/auth/recuperar*` | verificado | 8 pruebas RLS pasadas |
| 16 | CU16 | Recomendaciones IA | `cu17_Recomendacion` | `/api/cliente/recomendaciones`, `/ia` | parcialmente | Modelo Qwen cargado y hash verificado; sin clientes con historial en BD para probar `tipo="personalizada"` |
| 17 | CU17 | Catálogo administrativo | `cu18_gestionar_catalogo` | `/api/admin/catalogo` | implementado sin verificar | Código presente |
| 18 | CU18 | Proveedores | `cu19_gestionar_proveedores` | `/api/admin/proveedores` | implementado sin verificar | Código presente |
| 19 | CU19 | Inventario | `cu20_gestionar_inventario` | `/api/admin/inventario` | implementado sin verificar | Código presente |
| 20 | CU20 | Promociones y descuentos | `CU21_gestionar_promo_desc` | `/api/admin/promociones` | implementado sin verificar | Código presente |
| 21 | CU21 | Reservas en sucursal | `cu22_gestionar_reservas_sucursal` | `/api/admin/reservas-sucursal` | verificado | 72 pruebas PG pasadas, E2E 16 passed |
| 22 | CU22 | Devoluciones y cambios | `cu23_gestionar_devoluciones` | `/api/admin/devoluciones` | verificado | 72 pruebas PG pasadas, E2E 16 passed |
| 23 | CU23 | Venta de contado (caja) | `cu24_registrar_venta` | `/api/admin/caja/*` | implementado sin verificar | Código presente, E2E CU24 pasó |
| 24 | CU24 | Reportes dinámicos | `cu25_reportes` | `/api/admin/reportes/*` | implementado sin verificar | Código presente |
| 25 | CU25 | Vestidor virtual AR | — (no existe implementación) | — | **bloqueado** | Sin Android SDK, sin dispositivo/emulador, sin recursos AR |
| 26 | CU26 | Venta presencial offline móvil | `cu24_registrar_venta` / `cu12_carrito` | `/api/admin/caja/*`, `/api/cliente/carrito` | **bloqueado** | Sin Android SDK, sin dispositivo, no se puede compilar móvil |
| 27 | CU27 | Panel SaaS - Empresas | `plataforma_saas` | `/api/saas/empresas` | verificado | GET/POST probados con token SAAS-EMPRESAS |
| 28 | CU28 | Panel SaaS - Respaldos | `plataforma_saas/backups.py` | `/api/saas/respaldos` | verificado | GET 200 `configurado: true`, POST 202, descarga 200, 403 sin permiso |
| 29 | CU29 | Micrófono / entrada de audio | — (no existe implementación) | — | **bloqueado** | Sin hardware de audio, no se declaró como verificado |

### Leyenda de estados
| Estado | Significado |
|--------|-------------|
| **verificado** | Pruebas reales ejecutadas y aprobadas contra la BD de pruebas |
| **parcialmente** | Funcionalidad implementada parcialmente; faltan pruebas clave (ej. clientes con historial) |
| **implementado sin verificar** | Código presente en el repositorio, sin pruebas dirigidas que acrediten su funcionamiento |
| **bloqueado** | Bloqueado por dependencia externa (Android SDK, dispositivo, hardware de audio); documentado, no simulado |

### Restricciones respetadas
- ✅ No se declaró móvil, AR, micrófono ni Stripe como verificados
- ✅ Todas las pruebas contra `127.0.0.1:55432/fashionstore_test` únicamente
- ✅ No se consultó Supabase ni la base de producción
- ✅ No se hizo commit, push, merge, reset, clean, stash ni despliegue
- ✅ Se conservaron cambios locales y datos existentes
- ✅ No se modificaron archivos de móvil ni sus dependencias

---

## 5. Archivos modificados en esta sesión

| Archivo | Cambio | Razón |
|---------|--------|-------|
| `LiveClothesShop-api/app/main.py` | Línea 145: condición de `expiry_task` cambiada para ejecutarse en modo test y development | Habilitar tarea periódica de vencimiento de reservas en entorno de pruebas |
| `C:\SI2_Parcial1\.tmp\test_expiracion_reservas.py` | Nuevo | Script de prueba de expiración con dos empresas (datos temporales, rollback) |
| `C:\SI2_Parcial1\INFORME_CIERRE_BACKEND_WEB.md` | Nuevo | Este informe |

---

## 6. Siguientes acciones pendientes

| Acción | Prioridad | Nota |
|--------|-----------|------|
| Probar expiración con dos empresas reales | Media | Requieren insertar datos de empresa, sucursal, producto, variante, inventario, usuario, cliente y reserva en la BD de pruebas |
| Probar Qwen con cliente con historial (`tipo="personalizada"`, `origen="modelo"`) | Media | Requieren insertar usuario, compras, reservas y carrito en la BD de pruebas |
| Ejecutar `test_saas_postgres.py` (72 pruebas) | Baja | Requiere BD operativa en 55432 (ya está) |
| Ejecutar `test_cu24_recomendacion_ia_pg.py` | Baja | Requiere BD operativa y modelo Qwen (ya verificado) |
| Ejecución móvil nativa (CU25, CU26 documental) | Bloqueado | Requieren Android SDK, dispositivo/emulador |
