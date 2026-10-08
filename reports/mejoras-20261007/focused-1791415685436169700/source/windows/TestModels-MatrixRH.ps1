# Creado por Aldo Garcia.
<# Generacion y JSON sinteticos; no abre documentos ni altera cuentas/datos SQL. #>
[CmdletBinding()]
param(
    [ValidateSet("fast", "deep", "all")][string]$Profile = "all"
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "Common-MatrixRH.ps1")
Write-Section "MATRIX RH - PRUEBA REAL DE MODELOS"
if (-not (Test-MatrixIntegrity)) { exit 1 }
if (-not (Test-MatrixDependencies)) {
    Write-Fail "Instale primero las dependencias con INSTALAR_MATRIX_RH.bat."
    exit 1
}
if (-not (Start-MatrixOllamaIfNeeded)) { exit 1 }
$reportDirectory = Join-Path (Get-MatrixRoot) "var\reports\models"
New-Item -ItemType Directory -Path $reportDirectory -Force | Out-Null
$reportName = "models-" + (Get-Date -Format "yyyyMMdd-HHmmssfff") + "-" + [guid]::NewGuid().ToString("N") + ".json"
$reportPath = Join-Path $reportDirectory $reportName
Write-Host "  Se prueban embeddings, respuesta final y JSON con datos sinteticos."
Write-Host "  Se usan gemma4:latest y embeddinggemma:latest ya instalados en Ollama."
$code = Invoke-MatrixPython -Arguments @(
    "-m", "scripts.model_smoke_test", "--run-inference", "--profile", $Profile, "--output", $reportPath
)
if ($code -eq 0) {
    Write-Ok "Los modelos completaron las pruebas del adapter usado por el chat."
}
else {
    Write-Fail "Un modelo no completo su prueba. Revise failure_kind, finish_reason y output_token_limit."
}
Write-Host "  Informe local: $reportPath"
exit $code
