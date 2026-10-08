# Creado por Aldo Garcia.
<#
.SYNOPSIS
    Instalacion idempotente de Matrix RH en Windows.

.DESCRIPTION
    Pasos: detectar Python 3.12 x64, detectar Ollama y sus dos
    modelos, comprobar /api/embed y la dimension real, crear el venv, instalar
    dependencias, preparar el frontend, comprobar MySQL, aplicar migraciones,
    crear los seeds sinteticos (solo development/test), ejecutar preflight y
    lanzar la ingesta inicial.

    No sobrescribe una instalacion existente: el .env se conserva, el venv se
    reutiliza y las migraciones son idempotentes.
#>

[CmdletBinding()]
param(
    [switch]$SkipFrontend,
    [switch]$RebuildFrontend,
    [switch]$SkipIngest,
    [switch]$WithAutostart,
    [switch]$Offline
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "Common-MatrixRH.ps1")

$root = Get-MatrixRoot
$failed = $false

$operationLock = Enter-MatrixOperationLock
try {

Write-Section "MATRIX RH - INSTALACION"
Write-Host "  Raiz del proyecto: $root"
if ($SkipFrontend -and $RebuildFrontend) {
    Write-Fail "Use -RebuildFrontend para compilar o -SkipFrontend para reutilizar; son opciones excluyentes."
    exit 1
}

Write-Section "Integridad de la entrega (sin dependencias ni cambios)"
if (-not (Test-MatrixIntegrity)) {
    Write-Fail "La fuente no coincide con el ZIP o su version esta incompleta. No se modifica esta instalacion."
    Write-Host "         Extraiga el proyecto completo en una carpeta vacia; conserve .env, corpus, var y la base existente."
    exit 1
}

# No sincronizar paquetes ni abrir el indice embedded mientras otro proceso
# ocupa el puerto de esta configuracion. La prueba temporal de bind cierra
# siempre sus sockets y no escribe archivos ni detiene procesos existentes.
if (-not (Test-MatrixBackendPortFree)) {
    Write-Fail "El puerto del backend esta ocupado o no se pudo comprobar. La instalacion no se modifica."
    Write-Host "         Si esta instancia esta activa, ejecute DETENER_MATRIX_RH.bat y repita."
    Write-Host "         Si el puerto pertenece a otro servicio, revise APP_HOST y APP_PORT en .env."
    exit 1
}

# ---------------------------------------------------------------- 1. Python --
Write-Section "1/10  Python 3.12 x64"
$systemPython = $null
$launcher = Get-Command py -ErrorAction SilentlyContinue
if ($launcher) {
    $candidate = & py -3.12 -c "import sys; print(sys.executable)" 2>$null
    if ($LASTEXITCODE -eq 0 -and $candidate) { $systemPython = $candidate.Trim() }
}
if (-not $systemPython) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $systemPython = $cmd.Source }
}
if (-not (Assert-Python312 -PythonPath $systemPython)) { exit 1 }

# Los modelos se verifican DESPUES de cargar .env e instalar dependencias.
# El adapter Python es la fuente de verdad tambien para otros runtimes locales.

# ------------------------------------------------------------------ 3. .env --
Write-Section "2/10  Configuracion (.env)"
if (-not (Test-MatrixEnvFile)) { exit 1 }
if (-not (Initialize-MatrixSeedPassword)) { exit 1 }
New-MatrixRuntimeDirs

$wamp = Find-WampMySql
if ($wamp) { Write-Ok "WAMP detectado en $wamp (puede proveer MySQL/MariaDB)." }
else { Write-Warn "No se detecto WAMP; se usara el DATABASE_URL configurado en .env." }

# ------------------------------------------------------------------- 4. venv --
Write-Section "3/10  Entorno virtual"
$venvPython = Join-Path $root ".venv\Scripts\python.exe"
$rootMarker = Join-Path $root ".venv\matrix-root.txt"
if ((Test-Path $venvPython) -and ((-not (Test-Path $rootMarker)) -or ((Get-Content $rootMarker -Raw).Trim() -ne $root))) {
    $backup = ".venv.previous-" + (Get-Date -Format "yyyyMMddHHmmssfff")
    Rename-Item -Path (Join-Path $root ".venv") -NewName $backup
    Write-Warn "Entorno virtual anterior conservado como $backup; se creara uno para esta ruta."
}
if (Test-Path $venvPython) {
    if (-not (Assert-Python312 -PythonPath $venvPython)) {
        $backup = ".venv.previous-" + (Get-Date -Format "yyyyMMddHHmmssfff")
        Rename-Item -Path (Join-Path $root ".venv") -NewName $backup
        Write-Warn "Interprete anterior conservado como $backup; se creara Python 3.12 x64."
    }
    else { Write-Ok "Entorno virtual de esta ruta reutilizado; dependencias pendientes de comprobar." }
}
if (-not (Test-Path $venvPython)) {
    Write-Step "Creando .venv..."
    & $systemPython -m venv (Join-Path $root ".venv")
    if ($LASTEXITCODE -ne 0) { Write-Fail "No fue posible crear el entorno virtual."; exit 1 }
    Write-Ok "Entorno virtual creado."
}

Set-Content -Path $rootMarker -Value $root -Encoding UTF8

# ----------------------------------------------------------- 5. dependencias --
Write-Section "4/10  Dependencias de Python"
$uv = Initialize-MatrixUv -PythonPath $systemPython -Offline:$Offline
if (-not $uv) {
    Write-Fail "No se pudo preparar uv (rango compatible >=0.11.33,<0.13.0)."
    Write-Host "         Revise el error anterior. Puede preparar uv por su canal autorizado y repetir."
    exit 1
}
$uvVersionLines = @(& $uv.Source --version 2>$null)
$uvVersionExit = $LASTEXITCODE
$uvVersion = ($uvVersionLines -join "`n").Trim()
# Una version exacta bloqueaba uv 0.11.33 antes de instalar ningun paquete.
# La validacion compartida usa solo stdlib y funciona con el venv aun vacio.
# Conserva el control de formato/target y admite las ramas compatibles probadas.
if ($uvVersionExit -ne 0 -or $uvVersionLines.Count -ne 1) {
    Write-Fail "No se pudo obtener una unica linea valida de 'uv --version'."
    exit 1
}
$uvCheck = Invoke-MatrixPython -Arguments @("-m", "scripts.uv_runtime", "--banner", $uvVersion, "--target", "x86_64-pc-windows-msvc")
if ($uvCheck -ne 0) { exit 1 }
Write-Step "Sincronizando el backend desde uv.lock (--frozen)..."
$previousUvEnvironment = $env:UV_PROJECT_ENVIRONMENT
Push-Location (Join-Path $root "backend")
try {
    $env:UV_PROJECT_ENVIRONMENT = (Join-Path $root ".venv")
    $syncArgs = @("sync", "--frozen", "--no-dev")
    $syncArgs += @("--python", $venvPython, "--no-python-downloads")
    if ($Offline) { $syncArgs += "--offline" }
    & $uv.Source @syncArgs
    if ($LASTEXITCODE -ne 0) { Write-Fail "uv no pudo sincronizar backend/uv.lock."; exit 1 }
}
finally {
    $env:UV_PROJECT_ENVIRONMENT = $previousUvEnvironment
    Pop-Location
}
if (-not (Assert-Python312 -PythonPath $venvPython)) { exit 1 }
if (-not (Test-MatrixDependencies)) {
    Write-Fail "La sincronizacion termino pero el entorno no puede importar los paquetes del backend."
    exit 1
}
Write-Ok "Dependencias sincronizadas exactamente desde uv.lock."

Write-Step "Retirando entradas antiguas con respaldo; se conservan configuracion, corpus y datos..."
$code = Invoke-MatrixPython -Arguments @("-m", "scripts.clean_legacy_layout")
if ($code -ne 0) {
    Write-Fail "No se pudo completar el mantenimiento de archivos."
    Write-Host "         Revise code, detail, phase y path en el JSON anterior; los originales retirados tienen respaldo."
    exit 1
}

Write-Step "Aplicando ajustes operativos de 1.3.1 con respaldo de .env..."
$code = Invoke-MatrixPython -Arguments @("-m", "scripts.bootstrap", "upgrade-config")
if ($code -ne 0) { Write-Fail "No se pudo actualizar la configuracion; no se ejecutan migraciones ni ingesta."; exit 1 }

Write-Section "5/10  Modelos y embeddings configurados"
if (-not (Start-MatrixOllamaIfNeeded)) { exit 1 }
$code = Invoke-MatrixPython -Arguments @("-m", "scripts.bootstrap", "pin-models")
if ($code -ne 0) { Write-Fail "No se pudieron fijar/verificar los digests; no se ejecutan migraciones ni ingesta."; exit 1 }
if (-not (Test-OllamaModels)) { exit 1 }
if (-not (Test-MatrixApplication)) {
    Write-Fail "La aplicacion ASGI no puede cargarse; no se ejecutan migraciones ni ingesta."
    exit 1
}
if (-not (Test-MatrixSafety)) {
    Write-Fail "La configuracion o el cifrado no cumplen el perfil; no se ejecutan migraciones ni ingesta."
    exit 1
}
Write-Step "Probando Gemma 4 (unico modelo generativo) y EmbeddingGemma con datos sinteticos..."
$code = Invoke-MatrixPython -Arguments @("-m", "scripts.model_smoke_test", "--run-inference", "--profile", "all")
if ($code -ne 0) {
    Write-Fail "Los modelos estan registrados, pero no completaron la prueba de generacion. No se ejecutan migraciones ni ingesta."
    Write-Host "         Ejecute DIAGNOSTICO_MATRIX_RH.bat -ProbarModelos para guardar el diagnostico de tokens y terminacion."
    exit 1
}


# ---------------------------------------------------------------------------
# 6. Colision de nombre de base de datos.
#
# El .env nuevo apunta a `matrix_rh_131`. Si ya existe una base
# con ese nombre creada por OTRA aplicacion, Matrix RH no la va a tocar. Se
# detecta aqui y no en el paso 8: descubrirlo a mitad de la instalacion deja el
# proceso a medias y el mensaje se pierde entre la salida del resto de pasos.
#
# Por que DESPUES de instalar las dependencias y no antes: la comprobacion se
# hace con SQLAlchemy dentro del venv. Cuando este bloque estaba en el paso 3 el
# instalador moria con `ModuleNotFoundError: No module named 'sqlalchemy'` en un
# equipo limpio, porque el venv aun no tenia nada instalado.
#
# La resolucion es no destructiva: se elige un nombre libre y se escribe en el
# .env. Nunca se borra ni se modifica la base ajena, y siempre se avisa.
# ---------------------------------------------------------------------------
Write-Section "6/10  Base de datos destino"
$envFile = Join-Path $root ".env"
$dbUrl = Get-MatrixEnvValue -Key "DATABASE_URL"
if (-not $dbUrl) {
    Write-Fail "No hay DATABASE_URL; configure la conexion MySQL en .env antes de instalar."
    exit 1
}
else {
    $dbName = if ($dbUrl -match '/([A-Za-z0-9_]+)(\?|$)') { $Matches[1] } else { "" }

    if (-not $dbName) {
        Write-Fail "DATABASE_URL no tiene un nombre de base MySQL valido; revise .env."
        exit 1
    }
    else {
        Write-Step "Comprobando que la base '$dbName' este libre..."
        $estado = Test-MatrixDatabaseFree -DatabaseUrl $dbUrl -DatabaseName $dbName

        if ($estado.Detalle -like 'DESCONOCIDO:*') {
            Write-Fail "No fue posible comprobar MySQL ($($estado.Detalle)); no se ejecutan migraciones."
            Write-Host "         Abra WampServer e inicie MySQL/MariaDB. Revise host, puerto y credenciales en DATABASE_URL."
            Write-Host "         WAMP puede usar 3306 o 3307 segun el servicio; respete el puerto real."
            exit 1
        }
        elseif ($estado.Detalle -eq 'PROPIA') {
            Write-Ok "La base '$dbName' es de una instalacion previa de Matrix RH: se reutiliza."
        }
        elseif ($estado.Libre) {
            Write-Ok "La base '$dbName' esta libre."
        }
        elseif ($estado.Detalle -eq 'EXISTE_VACIA' -and
            @('1', 'true', 'yes', 'on', 't', 'y') -contains (Get-MatrixEnvValue -Key 'MATRIX_ADOPT_EXISTING_DATABASE' -Default 'false').ToLowerInvariant()) {
            Write-Warn "Adopcion de la base vacia '$dbName' solicitada por MATRIX_ADOPT_EXISTING_DATABASE."
            Write-Host "         El migrador volvera a verificar la propiedad antes de escribir."
        }
        else {
            # Dos motivos distintos para no usarla, con el mismo desenlace: se
            # busca otro nombre y la base del operador queda intacta.
            if ($null -ne [Environment]::GetEnvironmentVariable("DATABASE_URL")) {
                Write-Fail "DATABASE_URL del entorno apunta a una base ajena. Corrija esa variable con un nombre libre y repita."
                exit 1
            }
            if ($estado.Detalle -eq 'EXISTE_VACIA') {
                Write-Warn "La base '$dbName' YA EXISTE y no la creo Matrix RH."
                Write-Host "         Esta vacia ahora, pero pudo crearla otra aplicacion." -ForegroundColor Yellow
                Write-Host "         Matrix RH no se apropia de una base que no creo." -ForegroundColor Yellow
            }
            else {
                $ajenas = ($estado.Detalle -replace '^OCUPADA:', '')
                Write-Warn "La base '$dbName' YA EXISTE y contiene tablas de otra aplicacion:"
                Write-Host "         $ajenas" -ForegroundColor Yellow
                Write-Host "         Matrix RH no va a modificarla." -ForegroundColor Yellow
            }

            # Se busca el primer nombre libre: matrix_rh_app, _app2, _app3...
            $elegido = $null
            foreach ($sufijo in @("_app", "_app2", "_app3", "_app4", "_app5")) {
                $candidato = "$dbName$sufijo"
                $prueba = Test-MatrixDatabaseFree -DatabaseUrl $dbUrl -DatabaseName $candidato
                # Un veredicto DESCONOCIDO no significa "ocupada" ni "libre": se
                # descarta el candidato en lugar de apropiarse de el a ciegas.
                if ($prueba.Detalle -like 'DESCONOCIDO:*') { continue }
                if ($prueba.Detalle -eq 'LIBRE') { $elegido = $candidato; break }
            }

            if (-not $elegido) {
                Write-Fail "No se encontro un nombre de base libre a partir de '$dbName'."
                Write-Host "         Edite DATABASE_URL en .env con un nombre libre y vuelva a ejecutar."
                exit 1
            }

            $nuevaUrl = $dbUrl -replace "/$([regex]::Escape($dbName))(\?|$)", "/$elegido`$1"
            if (Set-MatrixEnvValue -Key 'DATABASE_URL' -Value $nuevaUrl) {
                Write-Ok "Matrix RH usara la base '$elegido' (escrito en .env)."
                Write-Host "         Su base '$dbName' queda intacta." -ForegroundColor Cyan
            }
            else {
                Write-Fail "No fue posible actualizar DATABASE_URL en .env."
                exit 1
            }
        }
    }
}

# -------------------------------------------------------------- 7. frontend --
Write-Section "7/10  Frontend"
$dist = Join-Path $root "frontend\dist\index.html"
if (-not $RebuildFrontend) {
    if (-not (Test-Path $dist)) {
        Write-Fail "Falta frontend/dist/index.html. Extraiga el ZIP completo o use -RebuildFrontend con Node y Corepack."
        exit 1
    }
    Write-Ok "Interfaz compilada incluida en el ZIP; no requiere Node ni Corepack para instalar/arrancar."
}
else {
    $node = Get-Command node -ErrorAction SilentlyContinue
    if (-not $node) { Write-Fail "Instale Node.js 22.13+ o 24 LTS para compilar la UI."; exit 1 }
    $nodeVersion = & $node.Source -p "process.versions.node"
    $nodeRuntime = [version]$nodeVersion
    if (($nodeRuntime.Major -eq 22 -and $nodeRuntime -lt [version]"22.13.0") -or
        ($nodeRuntime.Major -lt 24 -and $nodeRuntime.Major -ne 22)) {
        Write-Fail "Node.js $nodeVersion no cumple ^22.13.0 o >=24. Use Node 22/24 LTS."
        exit 1
    }
    $corepack = Get-Command corepack.cmd -ErrorAction SilentlyContinue
    if (-not $corepack) { Write-Fail "Instale Corepack por el canal corporativo para verificar el gestor fijado en package.json."; exit 1 }
    $previousNetwork = $env:COREPACK_ENABLE_NETWORK
    $previousPrompt = $env:COREPACK_ENABLE_DOWNLOAD_PROMPT
    $env:COREPACK_ENABLE_DOWNLOAD_PROMPT = "0"
    if ($Offline) { $env:COREPACK_ENABLE_NETWORK = "0" }
    Push-Location (Join-Path $root "frontend")
    try {
        $code = Invoke-MatrixPython -Arguments @("-m", "scripts.verify_supply_chain", "--lock-only")
        if ($code -ne 0) { Write-Fail "El lockfile no paso la politica de suministro; no se descargan paquetes."; exit 1 }
        if ($Offline) {
            Write-Warn "Modo offline: no se consultaron avisos npm actuales. Use solo una cache validada por TI."
        }
        else {
            & $corepack.Source npm audit --audit-level=low --include=dev --include=optional --include=peer
            if ($LASTEXITCODE -ne 0) { Write-Fail "npm audit encontro avisos o no pudo verificarlos; no se descargan paquetes."; exit 1 }
        }
        # Corepack verifica el hash de npm fijado en packageManager. npm ci
        # verifica el lockfile y su integridad sin ejecutar hooks de instalacion.
        $npmArgs = @("ci", "--ignore-scripts", "--no-audit", "--no-fund", "--include=dev", "--include=optional", "--include=peer")
        if ($Offline) { $npmArgs += "--offline" }
        & $corepack.Source npm @npmArgs
        if ($LASTEXITCODE -ne 0) { Write-Fail "No se instalaron las dependencias de la UI."; exit 1 }
        $code = Invoke-MatrixPython -Arguments @("-m", "scripts.verify_supply_chain")
        if ($code -ne 0) { Write-Fail "Fallo la verificacion de dependencias."; exit 1 }
        & $corepack.Source npm run build
        if ($LASTEXITCODE -ne 0) { Write-Fail "El frontend no compilo."; exit 1 }
    }
    finally {
        Pop-Location
        $env:COREPACK_ENABLE_NETWORK = $previousNetwork
        $env:COREPACK_ENABLE_DOWNLOAD_PROMPT = $previousPrompt
    }
}

# ------------------------------------------------------ 8. base de datos ----
Write-Section "8/10  Base de datos y migraciones"
$code = Invoke-MatrixPython -Arguments @("-m", "scripts.bootstrap", "migrate")
if ($code -ne 0) {
    Write-Fail "Las migraciones fallaron. Revise DATABASE_URL en .env y que MySQL este arriba."
    exit 1
}
Write-Ok "Migraciones aplicadas."

Write-Step "Creando usuarios sinteticos de prueba (solo development/test)..."
$code = Invoke-MatrixPython -Arguments @("-m", "scripts.bootstrap", "seed")
if ($code -ne 0) { Write-Fail "Fallo el seed. La omision por configuracion ya devuelve codigo 0."; exit 1 }
else { Write-Ok "Etapa de usuarios de prueba completada; solo aplica cuando .env habilita el modo local." }

# ------------------------------------------------------------- 9. preflight --
Write-Section "9/10  Preflight"
$code = Invoke-MatrixPython -Arguments @("-m", "scripts.preflight")
if ($code -ne 0) { Write-Fail "El preflight reporto fallos."; $failed = $true }

# -------------------------------------------------------------- 10. ingesta --
Write-Section "10/10  Ingesta inicial del conocimiento"
if ($failed) {
    Write-Warn "Ingesta inicial omitida porque el preflight fallo; primero corrija los servicios o la configuracion."
}
elseif ($SkipIngest) {
    Write-Warn "Ingesta inicial omitida por parametro."
}
else {
    $code = Invoke-MatrixPython -Arguments @("-m", "scripts.bootstrap", "ingest")
    if ($code -ne 0) { Write-Fail "La ingesta inicial reporto errores; revise el detalle anterior."; $failed = $true }
    else { Write-Ok "Conocimiento indexado." }
}

if ($WithAutostart -and -not $failed) {
    Write-Section "Autoarranque"
    & (Join-Path $PSScriptRoot "Install-Autostart.ps1")
    if ($LASTEXITCODE -ne 0) { Write-Fail "No se pudo registrar el autoarranque solicitado."; $failed = $true }
}

Write-Section "RESULTADO DE LA INSTALACION"
if ($failed) {
    Write-Fail "La instalacion termino con fallos. Corrija lo indicado y vuelva a ejecutar."
    exit 1
}
Write-Ok "Matrix RH quedo instalado."
Write-Host ""
Write-Host "  INSTALAR_MATRIX_RH.bat encadena el arranque y verifica /ready." -ForegroundColor Cyan
Write-Host "  Arranques posteriores: INICIAR_MATRIX_RH.bat" -ForegroundColor Cyan
Write-Host "  El seed solo crea credenciales sinteticas si el modo local de prueba esta habilitado en .env." -ForegroundColor Cyan
$seedEnabled = (Get-MatrixEnvValue -Key "LOCAL_TEST_SEED_USERS_ENABLED" -Default "true").ToLowerInvariant()
$appProfile = (Get-MatrixEnvValue -Key "APP_ENV" -Default "development").ToLowerInvariant()
if (@("development", "test") -contains $appProfile -and @("1", "true", "yes", "on", "t", "y") -contains $seedEnabled) {
    Write-Host "  Usuarios locales: Matrix (administrador), MatrixR1 (lector de prestaciones)." -ForegroundColor Cyan
    Write-Host "  Contrasena inicial: valor MATRIX_SEED_PASSWORD del archivo $root\.env" -ForegroundColor Cyan
    Write-Host "  Si el usuario ya existia, conserva su contrasena anterior." -ForegroundColor Cyan
}
Write-Host ""
exit 0
}
finally { Exit-MatrixOperationLock -Lock $operationLock }
