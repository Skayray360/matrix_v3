# Creado por Aldo Garcia.
<#
.SYNOPSIS
    Detiene el backend de Matrix RH de forma ordenada.

.DESCRIPTION
    Usa el PID registrado por Start-MatrixRH.ps1. Si ese archivo falta o el
    proceso ya no existe, busca por linea de comandos, pero NUNCA mata procesos
    de Python ajenos al proyecto.
#>

[CmdletBinding()]
param([switch]$Force)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "Common-MatrixRH.ps1")

$operationLock = Enter-MatrixOperationLock
try {
Write-Section "MATRIX RH - DETENER"

$pidFile = Get-MatrixPidFile
$stopped = $false

if (Test-Path $pidFile) {
    $recorded = (Get-Content $pidFile -Raw).Trim()
    if ($recorded -match '^\d+$') {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$recorded" -ErrorAction SilentlyContinue
        if (Test-MatrixOwnedProcess $process) {
            Write-Step "Deteniendo PID $recorded..."
            Stop-MatrixOwnedBackend -ProcessId ([int]$recorded) -Force:$Force
            $stopped = $true
        }
    }
    Remove-Item $pidFile -ErrorAction SilentlyContinue
}

# Siempre se revisan TODOS: el PID registrado no descarta otra consola propia.
$backendDir = Get-MatrixBackendDir
$candidates = Get-CimInstance Win32_Process |
    Where-Object { Test-MatrixOwnedWorker $_ }
foreach ($candidate in $candidates) {
    Write-Step "Deteniendo PID $($candidate.ProcessId) ($backendDir)..."
    Stop-MatrixOwnedBackend -ProcessId $candidate.ProcessId -Force:$Force
    $stopped = $true
}

$remaining = @(Get-CimInstance Win32_Process | Where-Object { Test-MatrixOwnedWorker $_ })
if ($remaining.Count -gt 0) {
    Write-Fail "Persisten procesos propios de Matrix RH; la parada no se completo."
    exit 1
}

if ($stopped) { Write-Ok "Matrix RH detenido." }
else { Write-Ok "Matrix RH no estaba en ejecucion." }
exit 0
}
finally { Exit-MatrixOperationLock -Lock $operationLock }
