# Creado por Aldo Garcia.
<# Traslada estado desde una entrega detenida a una extraccion nueva completa. #>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$PreviousRoot,
    [switch]$SkipFrontend,
    [switch]$RebuildFrontend,
    [switch]$Offline
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "Common-MatrixRH.ps1")
Write-Section "MATRIX RH - ACTUALIZACION 1.3.1"
$previous = (Resolve-Path -LiteralPath $PreviousRoot).Path
$oldPython = Join-Path $previous ".venv\Scripts\python.exe"
$running = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
    $_.ExecutablePath -and
    [string]::Equals($_.ExecutablePath, $oldPython, [System.StringComparison]::OrdinalIgnoreCase)
})
if ($running.Count -gt 0) {
    Write-Fail "La entrega anterior sigue activa. Ejecute su DETENER_MATRIX_RH.bat antes de trasladar datos."
    exit 1
}
if (-not (Test-MatrixIntegrity)) { exit 1 }
$python = Get-MatrixPython -PreferSystem
if (-not $python -or -not (Assert-Python312 -PythonPath $python)) { exit 1 }
$code = Invoke-MatrixPython -AllowSystemPython -Arguments @(
    "-m", "scripts.release_transfer", "--previous-root", $previous
)
if ($code -ne 0) { Write-Fail "El traslado no se completo. La carpeta anterior sigue disponible."; exit 1 }
$installArgs = @{}
if ($SkipFrontend) { $installArgs.SkipFrontend = $true }
if ($RebuildFrontend) { $installArgs.RebuildFrontend = $true }
if ($Offline) { $installArgs.Offline = $true }
& (Join-Path $PSScriptRoot "Install-MatrixRH.ps1") @installArgs
exit $LASTEXITCODE
