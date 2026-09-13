# LiveClothesShop API — CU01 y CU02

Backend FastAPI con SQLAlchemy 2/Psycopg 3, Argon2id y sesiones opacas.
Fuente única del esquema: `../database/schema.sql`, relativa a este repositorio.
La aplicación **no crea tablas, no ejecuta migraciones ni provisiona usuarios al iniciar**.

## Instalación local (PowerShell, Python 3.12)

Ejecutar desde `C:\SI2_Parcial1\LiveClothesShop-api`:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-test.txt
```

Para un entorno sin herramientas de prueba, instalar `requirements.txt`.
El manifiesto conserva las dependencias preexistentes y añade Argon2id.

## Variables de entorno

La aplicación lee el entorno del proceso. No carga `.env` automáticamente.

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

El esquema debe estar aplicado previamente a esa base mediante una operación
autorizada. `schema.sql` es un esquema de creación, no una migración sobre una
base existente. Este trabajo no ha aplicado SQL a PostgreSQL.

Solo después de verificar el destino, la provisión explícita siguiente escribe
el rol Cliente sin funciones ni cuentas administrativas. No fue ejecutada durante
la implementación:

```powershell
.\.venv\Scripts\python.exe -m app.bootstrap --crear-rol-cliente
```

Si el rol tiene funciones o está asignado a cuentas de tipo distinto de `C`,
la provisión y el registro fallan de forma segura. Es una restricción deliberada
de CU01/CU02; no hay permisos de negocio del Cliente implementados todavía.

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
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-proxy-headers
```

`--no-proxy-headers` conserva la IP del par conectado para bitácora. En un despliegue
con proxy se deberá configurar explícitamente qué proxies son confiables.
Nunca habilitar logging de cuerpos, cookies, Authorization o parámetros SQL.

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

Campos: `ci`, `nombres`, `apellidoPat`, `apellidoMat`, `sexo` (`M`/`F`), `correo`,
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
nunca en JSON. El digest SHA-256 solo se persiste en `sesion`.
La contraseña se verifica exclusivamente contra Argon2id. Una cuenta inexistente,
inactiva o con credenciales incorrectas recibe el mismo 401.

### GET /api/auth/me — 200

Misma respuesta pública que login. Verifica cookie, vencimiento, revocación,
usuario activo y rol/permisos actuales. Permite validar la infraestructura de CU02.
También puede verificar una sesión existente por `Authorization: Bearer ...`;
se rechaza presentar cookie y Authorization simultáneamente. No se implementa
emisión de credenciales móviles ni Flutter en esta etapa.

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
se compila el mapping con el dialecto PostgreSQL y se verifica la integridad del
esquema movido. SQLite no sustituye la validación futura en PostgreSQL de tipos,
bloqueos y concurrencia. No se ejecutan pruebas contra URLs tomadas del entorno.

## Alcance y pendientes

- Implementados: CU01, CU02 y escritura de eventos mínima; sin consulta de bitácora.
- Preparada únicamente en SQL: recuperación de contraseña.
- No implementados: CU03–CU09, Angular, Flutter, JWT ni refresh tokens.
- No existe todavía limitación distribuida de intentos. Debe configurarse en la
  infraestructura o implementarse antes de exposición pública del login.
- No hay índice único de correo normalizado aprobado: API normaliza escrituras,
  comprueba registros anteriores y atiende la restricción UNIQUE. Escrituras
  externas al backend deben respetar la misma política.
- Las sesiones anteriores permanecen válidas hasta expirar o estar marcadas como
  revocadas. No existe endpoint de logout en esta fase.
