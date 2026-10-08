# Creado por Aldo Garcia.
<#
.SYNOPSIS
    Arranca Matrix RH (backend + frontend servido same-origin) y abre el navegador.

.DESCRIPTION
    Seccion 39.3: preflight, verificacion de dependencias, arranque del backend,
    espera a /health y /ready, apertura del navegador, ruta de logs visible y
    proteccion frente a arranques duplicados.
#>

[CmdletBinding()]
param(
    [switch]$NoBrowser,
    [switch]$SkipPreflight,
    [int]$ReadyTimeoutSeconds = 180
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "Common-MatrixRH.ps1")

$operationLock = Enter-MatrixOperationLock
try {
$root = Get-MatrixRoot
$url = Get-MatrixBackendUrl

Write-Section "MATRIX RH - ARRANQUE"

Write-Section "Integridad de la entrega"
if (-not (Test-MatrixIntegrity)) {
    Write-Fail "La entrega esta incompleta o tiene archivos cruzados; el backend no se inicia."
    Write-Host "         Reemplace los archivos completos del ZIP en esta carpeta, conservando .env, corpus y var."
    exit 1
}

# --- ya esta arriba? ---------------------------------------------------------
if (Test-MatrixBackendUp) {
    # También reconoce un backend iniciado en primer plano; el PID del archivo
    # nunca sustituye comprobar quién posee realmente el puerto.
    $ownProcess = Get-MatrixListeningBackend
    if ($null -eq $ownProcess) {
        Write-Fail "El puerto responde como Matrix RH pero el proceso no pertenece a esta carpeta. Revise APP_PORT y el PID."
        exit 1
    }
    Set-Content -Path (Get-MatrixPidFile) -Value $ownProcess.ProcessId -Encoding ASCII
    if (-not (Test-MatrixBackendReady) -or -not (Test-Path (Join-Path $root "frontend\dist\index.html"))) {
        Write-Fail "El proceso existente no esta listo o falta la interfaz."
        exit 1
    }
    Write-Ok "Matrix RH ya esta en ejecucion en $url (no se duplica el proceso)."
    if (-not $NoBrowser) { Start-Process $url }
    exit 0
}

if (-not (Test-MatrixBackendPortFree)) {
    Write-Fail "El puerto configurado esta ocupado o no se pudo comprobar. No se inicia ni se modifica esta instalacion."
    Write-Host "         Revise APP_HOST y APP_PORT en .env o detenga el proceso que ocupa $url."
    exit 1
}

if (-not (Test-MatrixVenv)) {
    Write-Fail "No existe el entorno virtual."
    Write-Host "         Ejecute primero INSTALAR_MATRIX_RH.bat"
    exit 1
}

# Esta comprobacion es obligatoria incluso con -SkipPreflight: ese parametro
# omite sondas de servicios, no permite arrancar con paquetes ausentes.
Write-Section "Dependencias de Python"
if (-not (Test-MatrixDependencies)) {
    Write-Fail "El entorno virtual esta incompleto; Matrix RH no se arranca."
    exit 1
}
if (-not (Test-MatrixApplication) -or -not (Test-MatrixSafety)) {
    Write-Fail "El backend no cumple la carga ASGI o los controles del perfil; no se inicia."
    exit 1
}

New-MatrixRuntimeDirs
if (-not (Start-MatrixOllamaIfNeeded)) { exit 1 }

# --- preflight ---------------------------------------------------------------
if (-not $SkipPreflight) {
    Write-Section "Preflight"
    $code = Invoke-MatrixPython -Arguments @("-m", "scripts.preflight")
    if ($code -ne 0) {
        Write-Fail "El preflight fallo: Matrix RH no se arranca."
        Write-Host "         Ejecute DIAGNOSTICO_MATRIX_RH.bat para ver el detalle."
        exit 1
    }
}

# --- arranque ----------------------------------------------------------------
Write-Section "Arrancando el backend"
$logDir = Get-MatrixLogDir
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$outLog = Join-Path $logDir "backend-$stamp.log"
$errLog = Join-Path $logDir "backend-$stamp.err.log"

$python = Get-MatrixPython
$previousPythonPath = $env:PYTHONPATH
$env:PYTHONPATH = Get-MatrixBackendDir
try {
    $process = Start-Process -FilePath $python `
        -ArgumentList @("-m", "scripts.bootstrap", "serve", "--skip-preflight") `
        -WorkingDirectory (Get-MatrixBackendDir) `
        -RedirectStandardOutput $outLog `
        -RedirectStandardError $errLog `
        -WindowStyle Hidden `
        -PassThru
}
finally { $env:PYTHONPATH = $previousPythonPath }

Set-Content -Path (Get-MatrixPidFile) -Value $process.Id -Encoding ASCII
Write-Step "Proceso backend PID $($process.Id)"
Write-Step "Log de salida: $outLog"
Write-Step "Log de errores: $errLog"

# --- espera a /health --------------------------------------------------------
Write-Step "Esperando a que el backend responda..."
$deadline = (Get-Date).AddSeconds($ReadyTimeoutSeconds)
$healthy = $false
while ((Get-Date) -lt $deadline) {
    if ($process.HasExited) {
        Write-Fail "El backend termino inesperadamente (codigo $($process.ExitCode))."
        Write-Host "         Ultimas lineas del log de errores:"
        if (Test-Path $errLog) { Get-Content $errLog -Tail 20 | ForEach-Object { Write-Host "           $_" } }
        Stop-MatrixOwnedBackend -ProcessId $process.Id
        exit 1
    }
    if (Test-MatrixBackendUp -TimeoutSec 2) { $healthy = $true; break }
    Start-Sleep -Seconds 2
}

if (-not $healthy) {
    Write-Fail "El backend no respondio a /health en $ReadyTimeoutSeconds segundos."
    if (Test-Path $errLog) { Get-Content $errLog -Tail 20 | ForEach-Object { Write-Host "           $_" } }
    Stop-MatrixOwnedBackend -ProcessId $process.Id
    exit 1
}
Write-Ok "Backend vivo en $url"

# --- espera a /ready ---------------------------------------------------------
Write-Step "Verificando dependencias obligatorias (/ready)..."
$ready = $false
# El dispatcher recupera un dueño local muerto mediante PID + creación.
# Nunca se añade un cooldown igual al deadline de inferencia.
$readyBudget = $ReadyTimeoutSeconds
Write-Step "Espera maxima de readiness: $readyBudget s (dependencias reales, sin cooldown de reinicio)."
$deadline = (Get-Date).AddSeconds($readyBudget)
while ((Get-Date) -lt $deadline) {
    if ($process.HasExited) { break }
    if (Test-MatrixBackendReady -TimeoutSec 5) { $ready = $true; break }
    try {
        # Lee el cuerpo 503 sin esperar que expire una reserva desconocida.
        $details = Get-MatrixReadinessDetail
        $dispatcher = @($details.components | Where-Object { $_.name -eq 'chat_dispatcher' })
        if ($dispatcher.Count -and $dispatcher[0].detail -like 'bloqueo de inicio:*') {
            Write-Fail $dispatcher[0].detail
            break
        }
    }
    catch { } # Dependencias todavía arrancando: conserva el presupuesto normal.
    Start-Sleep -Seconds 3
}

if ($ready) {
    Write-Ok "Todas las dependencias obligatorias estan operativas."
}
else {
    Write-Warn "El backend esta vivo pero NO listo (/ready = false)."
    try {
        $detail = Invoke-RestMethod -Uri "$url/ready" -TimeoutSec 5
        foreach ($component in $detail.components) {
            if (-not $component.ok) { Write-Warn "  $($component.name): $($component.detail)" }
        }
    }
    catch { Write-Warn "  No fue posible leer el detalle de /ready." }
    Write-Host "         Ejecute DIAGNOSTICO_MATRIX_RH.bat para el diagnostico completo."
    Stop-MatrixOwnedBackend -ProcessId $process.Id
    exit 1
}

# --- frontend ----------------------------------------------------------------
$dist = Join-Path $root "frontend\dist\index.html"
if (-not (Test-Path $dist)) {
    Write-Warn "No hay build del frontend: solo estara disponible la API en $url/api/v1"
    Write-Host "         Repita INSTALAR_MATRIX_RH.bat para compilar desde package-lock.json."
    Stop-MatrixOwnedBackend -ProcessId $process.Id
    exit 1
}
else {
    Write-Ok "Interfaz servida same-origin desde $url"
}

if (-not $NoBrowser) {
    Start-Process $url
}

Write-Section "MATRIX RH EN EJECUCION"
Write-Host "  URL:      $url"
Write-Host "  Logs:     $logDir"
Write-Host "  Detener:  DETENER_MATRIX_RH.bat"
Write-Host ""
exit 0
}
finally { Exit-MatrixOperationLock -Lock $operationLock }
