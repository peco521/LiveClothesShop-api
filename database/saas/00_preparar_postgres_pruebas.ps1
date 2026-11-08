# FashionStore - Preparacion del PostgreSQL DE PRUEBAS (aislado).
#
# Que hace este script:
#   1. Crea (si falta) un cluster PostgreSQL propio en .tmp/pgdata, puerto 55432.
#   2. Crea la base fashionstore_test.
#   3. Aplica 01, 02 y 03 (creacion + parches del esquema SaaS NUEVO).
#   4. Aplica 001_versiones_planes.sql antes de los grants del rol HTTP.
#   5. Genera contrasenas locales para los roles de base de datos.
#   6. Aplica 04 (roles y permisos minimos).
#   7. Escribe .local/backend.env con las cadenas de conexion.
#
# Que NO hace:
#   - No toca Supabase ni la base actual del proyecto.
#   - No migra datos de la instalacion anterior (ver database/saas/README.md).
#   - No carga datos de demostracion (ver scripts/seed_demo.py del backend).
#
# Uso:
#   cd LiveClothesShop-api
#   powershell -ExecutionPolicy Bypass -File database/saas/00_preparar_postgres_pruebas.ps1
#   powershell -ExecutionPolicy Bypass -File database/saas/00_preparar_postgres_pruebas.ps1 -Recreate

param(
  [int]$Port = 55432,
  [string]$PgBin = 'C:\Program Files\PostgreSQL\17\bin',
  [string]$DbName = 'fashionstore_test',
  [switch]$Recreate
)

$ErrorActionPreference = 'Stop'
$apiRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$workspaceRoot = Split-Path -Parent $apiRoot
$sqlDir = $PSScriptRoot
$dataDir = Join-Path $workspaceRoot '.tmp\pgdata'
$localDir = Join-Path $workspaceRoot '.local'
$rolesEnv = Join-Path $localDir 'db_roles.env'
$backendEnv = Join-Path $localDir 'backend.env'

$psql = Join-Path $PgBin 'psql.exe'
$initdb = Join-Path $PgBin 'initdb.exe'
$postgres = Join-Path $PgBin 'postgres.exe'

if (-not (Test-Path $psql)) { throw "No se encontro psql en $PgBin" }
New-Item -ItemType Directory -Force -Path (Join-Path $workspaceRoot '.tmp') | Out-Null
New-Item -ItemType Directory -Force -Path $localDir | Out-Null

function Invoke-Psql {
  param([string]$Database = 'postgres', [string[]]$Arguments)
  & $psql -h 127.0.0.1 -p $Port -U postgres -w -d $Database -v ON_ERROR_STOP=1 @Arguments
  if ($LASTEXITCODE -ne 0) { throw "psql fallo (exit $LASTEXITCODE) en $Database" }
}

# --- 1. Cluster ---------------------------------------------------------
if ($Recreate -and (Test-Path $dataDir)) {
  Write-Host 'Eliminando cluster de pruebas anterior...'
  & (Join-Path $PgBin 'pg_ctl.exe') -D $dataDir -m immediate stop 2>$null | Out-Null
  Start-Sleep -Seconds 2
  Remove-Item -Recurse -Force $dataDir
}
if (-not (Test-Path $dataDir)) {
  Write-Host "initdb en $dataDir"
  & $initdb -D $dataDir -U postgres --auth-host=trust --auth-local=trust --encoding=UTF8 --locale=C | Out-Null
  if ($LASTEXITCODE -ne 0) { throw 'initdb fallo' }
  Add-Content -Path (Join-Path $dataDir 'postgresql.conf') -Value @"

# FashionStore pruebas
port = $Port
listen_addresses = '127.0.0.1'
fsync = off
full_page_writes = off
"@
}

# --- 2. Arranque --------------------------------------------------------
$ready = $false
try {
  & $psql -h 127.0.0.1 -p $Port -U postgres -w -tAc 'select 1' *> $null
  $ready = ($LASTEXITCODE -eq 0)
} catch { $ready = $false }

if (-not $ready) {
  Write-Host "Arrancando cluster en el puerto $Port"
  # Start-Process desacopla el proceso: pg_ctl muere si la sesion padre termina.
  Start-Process -FilePath $postgres -ArgumentList '-D', $dataDir -WindowStyle Hidden
  for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Milliseconds 500
    & $psql -h 127.0.0.1 -p $Port -U postgres -w -tAc 'select 1' *> $null
    if ($LASTEXITCODE -eq 0) { $ready = $true; break }
  }
}
if (-not $ready) { throw "El cluster de pruebas no responde en 127.0.0.1:$Port" }
Write-Host "Cluster de pruebas listo en 127.0.0.1:$Port"

# --- 3. Base de datos ---------------------------------------------------
$exists = ((& $psql -h 127.0.0.1 -p $Port -U postgres -w -tAc "select 1 from pg_database where datname='$DbName'") | Out-String).Trim()
if ($Recreate -and $exists -eq '1') {
  Invoke-Psql -Arguments @('-c', "select pg_terminate_backend(pid) from pg_stat_activity where datname='$DbName'")
  Invoke-Psql -Arguments @('-c', "DROP DATABASE $DbName")
  $exists = ''
}
if ($exists -ne '1') {
  Write-Host "Creando base $DbName"
  Invoke-Psql -Arguments @('-c', "CREATE DATABASE $DbName ENCODING 'UTF8'")
}

# --- 4. Esquema base y parches -----------------------------------------
# 01/02 no son idempotentes: solo se aplican sobre una base vacia. Los parches
# posteriores y el seed de demostracion SI deben poder repetirse.
$schemaApplied = ((& $psql -h 127.0.0.1 -p $Port -U postgres -w -d $DbName -tAc "select to_regclass('public.empresa') is not null") | Out-String).Trim()
if ($schemaApplied -eq 't') {
  Write-Host 'Esquema SaaS ya presente: se omiten 01, 02 y 03 (use -Recreate para recrear).'
} else {
  foreach ($script in @(
      '01_FashionStore_SaaS_TABLAS.sql',
      '02_FashionStore_SaaS_TRIGGERS_PA.sql',
      '03_FashionStore_SaaS_Plataforma_Sync.sql')) {
    Write-Host "Aplicando $script"
    Invoke-Psql -Database $DbName -Arguments @('-f', (Join-Path $sqlDir $script))
  }
}

# Las suscripciones existentes quedan asignadas a su configuración vigente
# antes de otorgar permisos de aplicación sobre las tablas de versiones.
$versionesAplicadas = ((& $psql -h 127.0.0.1 -p $Port -U postgres -w -d $DbName -tAc "select to_regclass('public.plan_version') is not null") | Out-String).Trim()
if ($versionesAplicadas -ne 't') {
  $versiones = Join-Path $apiRoot 'app\modules\saas_suscripciones\planes_funcionalidades\migrations\001_versiones_planes.sql'
  Write-Host 'Aplicando migración 001_versiones_planes.sql'
  Invoke-Psql -Database $DbName -Arguments @('-f', $versiones)
} else {
  Write-Host 'Versiones de planes ya aplicadas: se conserva el estado existente.'
}

# --- 5. Contrasenas locales de los roles -------------------------------
function New-TestPassword {
  # 32 bytes -> base64url sin caracteres problematicos para URL ni shell.
  $bytes = New-Object byte[] 32
  $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
  $rng.GetBytes($bytes)
  $rng.Dispose()
  [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

if (-not (Test-Path $rolesEnv)) {
  $app = New-TestPassword
  $provisioner = New-TestPassword
  @(
    '# Generado por 00_preparar_postgres_pruebas.ps1. NO versionar.',
    '# Cluster de PRUEBAS en 127.0.0.1:55432. No corresponde a Supabase.',
    "FASHIONSTORE_APP_PASSWORD=$app",
    "FASHIONSTORE_PROVISIONER_PASSWORD=$provisioner"
  ) | Set-Content -Path $rolesEnv -Encoding UTF8
  Write-Host "Contrasenas de roles escritas en $rolesEnv (no se muestran por consola)"
} else {
  Write-Host "Reutilizando contrasenas existentes en $rolesEnv"
}

$vars = @{}
Get-Content $rolesEnv | Where-Object { $_ -match '^\s*([A-Z_]+)\s*=\s*(\S+)\s*$' } | ForEach-Object {
  $vars[$matches[1]] = $matches[2]
}
$appPassword = $vars['FASHIONSTORE_APP_PASSWORD']
$provisionerPassword = $vars['FASHIONSTORE_PROVISIONER_PASSWORD']
if (-not $appPassword -or -not $provisionerPassword) { throw "Faltan contrasenas en $rolesEnv" }

# --- 6. Roles y permisos ----------------------------------------------
Write-Host 'Aplicando 04_FashionStore_SaaS_ROLES.sql'
Invoke-Psql -Database $DbName -Arguments @(
  '-v', "app_password=$appPassword",
  '-v', "provisioner_password=$provisionerPassword",
  '-f', (Join-Path $sqlDir '04_FashionStore_SaaS_ROLES.sql')
)

# --- 6.1 Devoluciones y cambios CU23 -------------------------------
Write-Host 'Aplicando 05_cu23_devoluciones_cambios.sql'
Invoke-Psql -Database $DbName -Arguments @(
  '-f', (Join-Path $sqlDir '05_cu23_devoluciones_cambios.sql')
)

# --- 7. Cadenas de conexion para el backend ---------------------------
@(
  '# Generado por 00_preparar_postgres_pruebas.ps1. NO versionar.',
  '# Base DE PRUEBAS local. No es Supabase ni la base de produccion.',
  '# Rol HTTP de empresa: NOBYPASSRLS, sin propiedad de tablas.',
  "TEST_DATABASE_URL=postgresql+psycopg://fashionstore_app:$appPassword@127.0.0.1:$Port/$DbName",
  '# Rol de plataforma/aprovisionamiento: BYPASSRLS, solo panel SaaS.',
  "TEST_PLATFORM_DATABASE_URL=postgresql+psycopg://fashionstore_provisioner:$provisionerPassword@127.0.0.1:$Port/$DbName"
) | Set-Content -Path $backendEnv -Encoding UTF8

Write-Host ''
Write-Host 'Listo. Siguiente paso (carga de demostracion):'
Write-Host "  cd $apiRoot; .venv/Scripts/python.exe scripts/seed_demo.py"
Write-Host "Cadenas de conexion: $backendEnv"
exit 0
