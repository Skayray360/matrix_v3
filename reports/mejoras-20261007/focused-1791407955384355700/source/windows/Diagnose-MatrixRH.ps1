# Creado por Aldo Garcia.
<#
.SYNOPSIS
    Diagnostico de solo lectura de Matrix RH.

.DESCRIPTION
    Seccion 39.4. NO modifica nada: no crea directorios, no aplica migraciones,
    no arranca procesos. Delega en `scripts.preflight --read-only` y anade la
    parte que solo tiene sentido en Windows (procesos, puertos, logs recientes).
    -ProbarModelos es una opcion activa explicita y separada del modo de lectura.
#>

[CmdletBinding()]
param(
    [switch]$Json,
    [switch]$ProbarModelos,
    [ValidateSet("fast", "deep", "all")][string]$Profile = "all",
    [string]$Usuario = "Matrix"
)

Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot "Common-MatrixRH.ps1")

$root = Get-MatrixRoot
$url = Get-MatrixBackendUrl

if ($Json -and $ProbarModelos) {
    Write-Error "-Json es diagnostico de solo lectura; use -ProbarModelos por separado."
    exit 1
}
if ($ProbarModelos) {
    # Prueba activa solo por peticion explicita y sin competir con el backend.
    $operationLock = Enter-MatrixOperationLock
    try {
        if (-not (Test-MatrixBackendPortFree)) {
            Write-Fail "Detenga Matrix RH antes de la prueba activa de modelos."
            exit 1
        }
        & (Join-Path $PSScriptRoot "TestModels-MatrixRH.ps1") -Profile $Profile
        exit $LASTEXITCODE
    }
    finally { Exit-MatrixOperationLock -Lock $operationLock }
}

# ---------------------------------------------------------------------------
# Sin entorno virtual solo se pueden comprobar requisitos externos.
#
# `scripts.preflight` importa pydantic, sqlalchemy y el resto del backend. Si el
# operador ejecuta el diagnostico ANTES de instalar, la version anterior moria
# con `ModuleNotFoundError: No module named 'pydantic'`: un traceback que no
# dice lo unico que hace falta saber, que Matrix RH no esta instalado todavia.
#
# Esta guardia detecta el interprete ausente. Si existe pero faltan paquetes,
# preflight los reporta y se detiene antes de importar la configuracion.
# Se corta aqui, se dice que falta, y se comprueban solo las dos cosas que no
# dependen del venv: que MySQL y Ollama esten escuchando. Asi el operador ya
# sabe si tendra problemas ANTES de lanzar el instalador.
# ---------------------------------------------------------------------------
if (-not (Test-MatrixVenv)) {
    if ($Json) {
        [ordered]@{
            ok      = $false
            error   = "venv_ausente"
            detalle = "Matrix RH no esta instalado en este equipo (falta .venv)."
            accion  = "Ejecute INSTALAR_MATRIX_RH.bat"
        } | ConvertTo-Json -Compress
        exit 1
    }

    Write-Section "MATRIX RH - DIAGNOSTICO INCOMPLETO"
    Write-Host "  Raiz:   $root"
    Write-Fail "Matrix RH no esta instalado en este equipo: falta el entorno virtual (.venv)."
    Write-Host "         Ejecute INSTALAR_MATRIX_RH.bat y repita el diagnostico despues."
    Write-Host "         Las comprobaciones que necesitan el backend se omiten." -ForegroundColor DarkYellow

    Write-Section "Requisitos externos (lo unico comprobable sin instalar)"
    try { $endpoints = @(Get-MatrixServiceEndpoints | Where-Object { $_.Name -ne "Backend" }) }
    catch { $endpoints = @(); Write-Warn "Revise los endpoints de .env; no se pudo leer su host y puerto." }
    foreach ($item in $endpoints) {
        $ok = (Test-NetConnection -ComputerName $item.HostName -Port $item.Port -WarningAction SilentlyContinue).TcpTestSucceeded
        if ($ok) { Write-Ok "$($item.Name) escuchando en $($item.HostName):$($item.Port)" }
        else { Write-Warn "$($item.Name) sin escucha en $($item.HostName):$($item.Port) (el instalador lo necesitara)" }
    }

    Write-Section "FIN DEL DIAGNOSTICO"
    exit 1
}

if ($Json) {
    exit (Invoke-MatrixPython -Arguments @("-m", "scripts.preflight", "--read-only", "--json"))
}

Write-Section "MATRIX RH - DIAGNOSTICO"
Write-Host "  Raiz:   $root"
Write-Host "  Backend: $url"

# --- entorno virtual ---------------------------------------------------------
Write-Section "Entorno de ejecucion"
Write-Ok "Interprete del entorno virtual presente; las dependencias se verifican a continuacion."

$wamp = Find-WampMySql
if ($wamp) { Write-Ok "WAMP detectado: $wamp" } else { Write-Warn "WAMP no detectado." }

# --- preflight ---------------------------------------------------------------
Write-Section "Comprobaciones del sistema"
$code = Invoke-MatrixPython -Arguments @("-m", "scripts.preflight", "--read-only")

# --- estado del servicio -----------------------------------------------------
Write-Section "Estado del servicio"
if (Test-MatrixBackendUp) {
    Write-Ok "Backend vivo (/health)."
    if (Test-MatrixBackendReady) {
        Write-Ok "Backend listo (/ready)."
    }
    else {
        Write-Warn "Backend vivo pero no listo. Componentes con problema:"
        try {
            $detail = Invoke-RestMethod -Uri "$url/ready" -TimeoutSec 5
            foreach ($component in $detail.components) {
                if (-not $component.ok) { Write-Warn "  $($component.name): $($component.detail)" }
            }
        }
        catch { Write-Warn "  No fue posible leer /ready." }
    }
}
else {
    Write-Warn "El backend no responde (puede estar detenido a proposito)."
}

# --- procesos ----------------------------------------------------------------
Write-Section "Procesos de Matrix RH"
$procesos = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { Test-MatrixOwnedProcess $_ }
if ($procesos) {
    foreach ($proceso in $procesos) {
        Write-Host "  PID $($proceso.ProcessId)  inicio $($proceso.CreationDate)"
    }
}
else { Write-Host "  (ninguno)" }

# --- puertos -----------------------------------------------------------------
Write-Section "Puertos"
try { $endpoints = @(Get-MatrixServiceEndpoints) }
catch { $endpoints = @(); Write-Warn "Revise los endpoints de .env; no se pudo leer su host y puerto." }
foreach ($item in $endpoints) {
    $ok = (Test-NetConnection -ComputerName $item.HostName -Port $item.Port -WarningAction SilentlyContinue).TcpTestSucceeded
    if ($ok) { Write-Ok "$($item.Name) escuchando en $($item.HostName):$($item.Port)" }
    else { Write-Warn "$($item.Name) sin escucha en $($item.HostName):$($item.Port)" }
}
if ((Get-MatrixEnvValue -Key "QDRANT_MODE" -Default "embedded") -eq "embedded") {
    Write-Ok "Qdrant embedded: no necesita un servidor escuchando en 6333; su almacen se comprueba en preflight o /ready."
}

# --- scheduler ---------------------------------------------------------------
Write-Section "Scheduler de reconciliacion (24 h)"
if ($code -eq 0) {
    Invoke-MatrixPython -Arguments @("-m", "scripts.bootstrap", "status", "--read-only") | Out-Null
}
else {
    Write-Warn "Comprobacion omitida: primero resuelva los fallos del preflight."
}

# --- logs --------------------------------------------------------------------
Write-Section "Logs recientes"
$logDir = Get-MatrixLogDir
if (Test-Path $logDir) {
    $recientes = Get-ChildItem $logDir -Filter "*.log" | Sort-Object LastWriteTime -Descending | Select-Object -First 3
    foreach ($log in $recientes) {
        Write-Host "  $($log.Name)  ($([int]($log.Length / 1KB)) KB, $($log.LastWriteTime))"
    }
    if (-not $recientes) { Write-Host "  (sin logs)" }
}
else { Write-Host "  (directorio de logs no creado aun)" }

Write-Section "Modelos locales: configuracion efectiva y hardware (sin inferencia)"
$modelCode = Invoke-MatrixPython -Arguments @("-m", "scripts.local_model_diagnostics")
if ($modelCode -ne 0) { $code = 1 }

Write-Section "Fuentes autorizadas, indice y reserva del dispatcher (solo lectura)"
$ragCode = Invoke-MatrixPython -Arguments @("-m", "scripts.diagnosticar_rag", "--root", $root, "--username", $Usuario)
if ($ragCode -ne 0) { $code = 1 }

Write-Section "FIN DEL DIAGNOSTICO"
exit $code
