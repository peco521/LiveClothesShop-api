# LiveClothesShop API

Backend FastAPI con SQLAlchemy 2/Psycopg 3, Argon2id y accesos opacos en memoria.
El usuario se mapea a `usuario.nombre`, sin `usuario.activo` ni tabla `sesion`.
No se necesita el archivo externo `../database/schema.sql` para arrancar la API.
La aplicación **no crea tablas, no ejecuta migraciones ni provisiona usuarios al iniciar**.
Las decisiones y limitaciones del rediseño se explican en
[Autenticación sin tabla de sesiones](docs/autenticacion-sin-tabla.md).

## Instalación local (PowerShell, Python 3.12)

Ejecutar desde la raíz de este repositorio:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-test.txt
```

Para un entorno sin herramientas de prueba, instalar `requirements.txt`.
El manifiesto conserva las dependencias preexistentes y añade Argon2id.

## Variables de entorno

La aplicación carga el archivo `.env` de este repositorio. Las variables del
entorno del proceso tienen prioridad; `.env` no debe versionarse ni compartirse.

| Variable | Obligatoria / valor predeterminado | Uso |
|---|---|---|
| `DATABASE_URL` | Obligatoria | URL `postgresql+psycopg://...` con credenciales reales configuradas fuera del código |
| `CLIENTE_ROL_ID` | Obligatoria; máximo 15 caracteres | Identificador del rol público, controlado por el servidor |
| `SESSION_HOURS` | `8` | Duración absoluta de la sesión, entre 1 y 168 horas |
| `ENVIRONMENT` | `development` | `development`, `production` o `test` |
| `COOKIE_SECURE` | `false`; obligatorio `true` en producción | Cookie exclusiva de HTTPS |
| `COOKIE_SAMESITE` | `lax` | `lax`, `strict` o `none`; `none` requiere Secure |
| `COOKIE_NAME` | `liveclothes_session` | Nombre de cookie HttpOnly, ruta `/api` y sin Domain |
| `ALLOWED_ORIGINS` | `["http://localhost:4200","http://localhost:4300"]` | Lista JSON de orígenes exactos; HTTPS en producción |

### Un solo código, dos entornos

`ENVIRONMENT` decide el escenario y con él los valores externos; no hay
`localhost`, dominios ni claves escritos en la lógica:

- `ENVIRONMENT=development` (o `test`) → Stripe en **modo prueba** (`sk_test_...`).
- `ENVIRONMENT=production` → Stripe en **modo vivo** (`sk_live_...`).

La combinación contraria (`development` + `sk_live_...`, `production` +
`sk_test_...`) **aborta el arranque** antes de cualquier llamada de red, y los
mensajes de error nunca incluyen el valor de las claves. La configuración LIVE se
define en las variables del entorno del servidor (Render), nunca dentro del
`.env` local ni del repositorio. Detalles y variables de pago en
[Stripe](docs/CU13_CU14_STRIPE.md) y en la plantilla `.env.example`.

Ejemplo local con datos ficticios: sustituir el valor de `DATABASE_URL` por la
configuración privada de una base **local** ya preparada. No compartir la URL real.

```powershell
$env:DATABASE_URL = 'postgresql+psycopg://usuario_local:CAMBIAR_LOCALMENTE@localhost:5432/liveclothesshop'
$env:CLIENTE_ROL_ID = 'cliente'
$env:SESSION_HOURS = '8'
$env:ENVIRONMENT = 'development'
$env:COOKIE_SECURE = 'false'
$env:ALLOWED_ORIGINS = '["http://localhost:4200","http://localhost:4300","http://127.0.0.1:8000"]'
```

Las tablas de negocio deben existir previamente en esa base. No ejecutar un
script de creación sobre una base existente sin revisar sus efectos. El rediseño
de autenticación no requiere agregar `activo`, `nombres` ni `sesion` a PostgreSQL.
Para una base existente deben ejecutarse, en el orden documentado, los scripts
de `database/sql/`; esta versión requiere especialmente
`04_preparar_procedimientos_backend.sql`.

Solo después de verificar el destino, la provisión explícita siguiente escribe
el rol Cliente sin funciones ni cuentas administrativas. No fue ejecutada durante
la implementación:

```powershell
.\.venv\Scripts\python.exe -m app.bootstrap --crear-rol-cliente
```

Si el rol tiene funciones o está asignado a cuentas de tipo distinto de `C`,
la provisión y el registro fallan de forma segura. Es una restricción deliberada
de CU01/CU02; las rutas de compra validan además el perfil de Cliente.

Para crear el primer SuperAdmin en una base local ya preparada, configurar las
variables de entorno anteriores y ejecutar:

```powershell
.\.venv\Scripts\python.exe -m app.bootstrap --crear-superadmin
```

El comando solicita correo, datos del perfil, `cod_adm` y una contraseña nueva
dos veces en la terminal. No existe una contraseña predeterminada ni se imprime
la contraseña; las credenciales deben definirlas y conservarlas los responsables
del entorno. Si el correo ya pertenece a un SuperAdmin compatible, el comando
verifica el registro y no cambia su contraseña.

Arranque:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --no-proxy-headers
```

`--no-proxy-headers` conserva la IP del par conectado para bitácora. En un despliegue
con proxy se deberá configurar explícitamente qué proxies son confiables.
Nunca habilitar logging de cuerpos, cookies, Authorization o parámetros SQL.
Usar una sola instancia/worker: los accesos viven en memoria. Un reinicio,
incluido un reinicio por `--reload`, exige iniciar sesión de nuevo.

## Contratos

Todas las respuestas de autenticación llevan `Cache-Control: no-store`.
Las peticiones POST necesitan:

- `Content-Type: application/json`
- `Origin`: uno de los orígenes configurados
- `X-CSRF-Protection: 1`

La defensa CSRF utiliza una cabecera personalizada y comprobación exacta de
Origin, incluido el login. La cabecera es un marcador, no un secreto ni un token.
CORS solo permite los orígenes configurados y credenciales explícitas.
Una llamada desde Swagger también debe enviar estas cabeceras; la UI por defecto
no configura automáticamente el marcador.

### POST /api/auth/registro — 201

Campos: `ci`, `nombre` (también se acepta `nombres`), `apellidoPat`, `apellidoMat`, `sexo` (`M`/`F`), `correo`,
`telefono`, `direccion`, `fechaNac` (ISO), `contrasena` (12–128 caracteres, no vacía).
Nombre/apellidos, teléfono y dirección no pueden quedar en blanco. No se cambia
ni trunca la contraseña. Los campos adicionales, incluidos rol, tipo y permisos,
se rechazan con 422. No se inventa un campo de confirmación de contraseña en la API.

Respuesta: `idUsuario`, `correo` normalizado y `mensaje`.
No establece una sesión. Usuario, Cliente y evento se confirman en una transacción.
`cod_cl` tiene 10 caracteres aleatorios; la identidad única es `idusuario`, conforme
al esquema oficial que no declara UNIQUE sobre `cod_cl`.

### POST /api/auth/login — 200

Entrada: `correo`, `contrasena`.
Respuesta:

```json
{
  "usuario": {"idUsuario": "identificador", "nombres": "Ana", "correo": "ana@example.com"},
  "rol": {"nro": "cliente", "descripcion": "Cliente"},
  "permisos": [],
  "expiraEn": "2026-09-11T20:00:00Z"
}
```

La credencial aleatoria se entrega únicamente mediante `Set-Cookie` HttpOnly;
nunca en JSON. Su digest SHA-256 se conserva únicamente en memoria del servidor.
La contraseña se verifica exclusivamente contra Argon2id. Una cuenta inexistente,
con contraseña en texto plano o con credenciales incorrectas recibe el mismo 401.
El JSON mantiene el alias `nombres` para el frontend existente; no es una columna SQL.

### GET /api/auth/me — 200

Misma respuesta pública que login. Verifica cookie, vencimiento, revocación,
existencia del usuario, huella de su contraseña y rol/permisos actuales.
También puede verificar una sesión existente por `Authorization: Bearer ...`;
se rechaza presentar cookie y Authorization simultáneamente. No se implementa
emisión de credenciales móviles ni Flutter en esta etapa.

### POST /api/auth/logout — 204

Invalida el acceso actual en memoria y elimina su cookie. Otros dispositivos
continúan autenticados hasta salir, expirar o cambiar la contraseña. Si falla
la auditoría, la operación no se confirma y devuelve un error controlado.

### Errores

```json
{"error": {"code": "autenticacion_rechazada", "message": "No se pudo autenticar la solicitud"}}
```

- 401: autenticación o sesión inválida.
- 403: origen/cabecera CSRF inválidos o permiso insuficiente.
- 409: correo duplicado.
- 422: entrada inválida; nunca incluye el payload en el error.
- 503: rol público no disponible o fallo controlado de persistencia.
- 500: error inesperado sin datos internos.

## Pruebas sin PostgreSQL externo

```powershell
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
.\.venv\Scripts\python.exe -m pip check
```

Las pruebas inyectan SQLite en memoria, activan FK y verifican transacciones reales,
contratos HTTP y Argon2id. La URL PostgreSQL del fixture nunca se conecta. También
se compila el mapping con el dialecto PostgreSQL y se comprueba que no dependa
de las columnas o tabla retiradas. SQLite no sustituye la validación en PostgreSQL de tipos,
bloqueos y concurrencia. No se ejecutan pruebas contra URLs tomadas del entorno.

## Alcance y pendientes

- El frontend separa `/login` (clientes) y `/admin/login` (administradores y
  empleados). Los endpoints `/api/auth/login/cliente` y `/api/auth/login/admin`
  validan el tipo de cuenta; `/api/auth/login` se conserva para consumidores
  anteriores. La identidad devuelve `usuario.tipo`; las operaciones internas
  siguen exigiendo sus permisos CU05–CU09.
- La configuración local usa `CLIENTE_ROL_ID=C`, correspondiente al rol real de
  Supabase. El script 05 documenta la reparación aplicada al perfil ADM-002 y
  los permisos administrativos del rol A; no modifica contraseñas.
- Las cuentas nuevas guardan las contraseñas con Argon2id. Las cuentas antiguas
  importadas en texto plano (administrador, cliente o empleado) se convierten
  automáticamente al ingresar con su contraseña correcta. En PostgreSQL se usa
  `sp_cambiar_contrasena`; la conversión y la bitácora se confirman juntas antes
  de entregar la cookie. Los formatos de hash reconocidos pero no compatibles
  requieren recuperación de contraseña. El login no asigna permisos ni roles.
- El repositorio incluye autenticación, administración y compra hasta CU15.
- CU13/CU14 incluyen cancelación y Stripe Checkout (USD) con el mismo código en
  modo prueba (desarrollo local) y modo vivo (producción); el modo se deriva de
  `ENVIRONMENT`. Consulte [configuración y límites Stripe](docs/CU13_CU14_STRIPE.md).
- La recuperación incluye un adaptador Gmail API configurable en `.env`;
  consulte [CU04: configuración Gmail](docs/CU04_GMAIL.md) para obtener credenciales OAuth.
  la tabla `recuperacion_contrasena` forma parte del SQL corregido y del script 04.
- La autenticación de usuarios no utiliza JWT ni refresh tokens.
  Gmail utiliza un refresh token OAuth separado, exclusivo de la cuenta remitente.
- No existe todavía limitación distribuida de intentos. Debe configurarse en la
  infraestructura o implementarse antes de exposición pública del login.
- El script 04 crea un índice único sobre `lower(btrim(correo))`; si encuentra
  duplicados normalizados se detiene para que sean corregidos explícitamente.
- No existe activación/desactivación de cuentas. Las rutas antiguas `/estado`
  de usuarios y `/estado-cuenta` de clientes fueron retiradas. El frontend
  administrativo debe retirar esos controles; no se modificó en este cambio.
- Las pruebas usan los modelos del repositorio para crear sus bases aisladas;
  no necesitan un archivo `database/schema.sql` externo.
