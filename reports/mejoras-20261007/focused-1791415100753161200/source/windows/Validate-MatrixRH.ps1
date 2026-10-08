# Creado por Aldo Garcia.
<#
.SYNOPSIS
    Validacion completa con evidencia reproducible (seccion 39.5).

.DESCRIPTION
    Secuencia obligatoria:
      1. preflight
      2. migraciones
      3. seed de prueba
      4. frescura del RAG (reconciliacion incremental)
      5. prueba de embeddings y dimension
      6. tests de backend (unit + integration)
      7. tests de seguridad
      8. golden set del RAG
      9. Playwright E2E (arranca el backend, ejecuta y lo detiene)
     10. escenarios Matrix
     11. escenarios MatrixR1
     12. escaneo de secretos
     13. resumen del quality gate

    El orden importa: los pasos 1-8 acceden al indice vectorial directamente y el
    almacen embebido admite un unico proceso, por lo que el backend debe estar
    DETENIDO hasta el paso 9.

    Genera reports/final-validation.json.
#>

[CmdletBinding()]
param(
    [switch]$SkipE2E,
    [switch]$QuickRag  # evalua el golden set solo en modo recuperacion
)

Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot "Common-MatrixRH.ps1")

# Corepack verifica el gestor npm fijado tambien durante audit y E2E.
$env:COREPACK_ENABLE_DOWNLOAD_PROMPT = "0"

$root = Get-MatrixRoot
$reportsDir = Join-Path $root "reports"
$testsDir = Join-Path $reportsDir "tests"
New-Item -ItemType Directory -Force -Path $testsDir | Out-Null

$results = [ordered]@{}
$startedAt = Get-Date

function Register-Step {
    param([string]$Name, [int]$ExitCode, [string]$Detail = "")
    $status = if ($ExitCode -eq 0) { "PASS" } else { "FAIL" }
    $results[$Name] = [ordered]@{ status = $status; exit_code = $ExitCode; detail = $Detail }
    if ($ExitCode -eq 0) { Write-Ok "$Name : PASS" } else { Write-Fail "$Name : FAIL (codigo $ExitCode)" }
}

function Check-PytestCompleteness {
    param([string]$Name, [string]$Report)
    if ($results[$Name].status -ne "PASS") { return }
    try {
        [xml]$junit = Get-Content -LiteralPath $Report -Raw
        $count = 0; $skipped = 0
        foreach ($suite in $junit.SelectNodes("//testsuite")) {
            $count += [int]$suite.GetAttribute("tests")
            $skipped += [int]$suite.GetAttribute("skipped")
        }
        if ($count -eq 0 -or $skipped -gt 0) {
            $results[$Name].status = "INCOMPLETE"
            $results[$Name].detail = "$count casos recopilados, $skipped omitidos; suite no validada por completo"
        }
    }
    catch { Register-Step $Name 1 "reporte JUnit ausente o invalido" }
}

Write-Section "MATRIX RH - VALIDACION COMPLETA"

# La integracion incluye DELETE con COMMIT. Bloquear antes de detener servicios,
# migrar o ingerir: esta validacion solo opera sobre una copia de pruebas.
$testEnvironmentExit = Invoke-MatrixPython -Arguments @("-m", "scripts.test_environment")
if ($testEnvironmentExit -ne 0) {
    Write-Fail "Use APP_ENV=test, una BD matrix_rh_test dedicada y MATRIX_TEST_ALLOW_DESTRUCTIVE=true. No se modificaron datos ni se detuvo el backend."
    exit 1
}

# No ejecutar Playwright/otras herramientas JS antes de inspeccionar el arbol.
$frontendTrusted = $false
Register-Step "cadena_de_suministro" (Invoke-MatrixPython -Arguments @(
    "-m", "scripts.verify_supply_chain", "--output", (Join-Path $reportsDir "security\supply_chain.json")
))
if ($results["cadena_de_suministro"].status -eq "PASS" -and (Get-Command corepack -ErrorAction SilentlyContinue)) {
    Push-Location (Join-Path $root "frontend")
    try {
        & corepack npm audit --omit=dev --audit-level=low
        Register-Step "npm_audit_produccion" $LASTEXITCODE
        & corepack npm audit --audit-level=low --include=dev --include=optional --include=peer
        Register-Step "npm_audit_completo" $LASTEXITCODE
        $frontendTrusted = ($results["npm_audit_produccion"].status -eq "PASS" -and $results["npm_audit_completo"].status -eq "PASS")
    }
    finally { Pop-Location }
}
else {
    $results["npm_audit_completo"] = [ordered]@{ status = "SKIPPED"; exit_code = 0; detail = "arbol bloqueado o Corepack ausente" }
}

# Se detiene el backend: los pasos siguientes necesitan acceso exclusivo al indice.
& (Join-Path $PSScriptRoot "Stop-MatrixRH.ps1") | Out-Null

Write-Section "1/13  Preflight"
Register-Step "preflight" (Invoke-MatrixPython -Arguments @("-m", "scripts.preflight", "--output", (Join-Path $testsDir "preflight.json")))

Write-Section "2/13  Migraciones"
Register-Step "migraciones" (Invoke-MatrixPython -Arguments @("-m", "scripts.bootstrap", "migrate"))

Write-Section "3/13  Seed de identidad de prueba"
Register-Step "seed" (Invoke-MatrixPython -Arguments @("-m", "scripts.bootstrap", "seed"))

Write-Section "4/13  Frescura del RAG"
Register-Step "rag_freshness" (Invoke-MatrixPython -Arguments @("-m", "scripts.bootstrap", "ingest"))

Write-Section "5/13  Embeddings y dimension"
Register-Step "embeddings" (Invoke-MatrixPython -Arguments @("-m", "scripts.preflight", "--llm-only")) "modelos, endpoint y dimension desde .env"

Write-Section "6/13  Tests de backend (unit + integration)"
Register-Step "tests_backend" (Invoke-MatrixPython -Arguments @(
        "-m", "pytest", "tests/unit", "tests/integration", "-q",
        "--junitxml", (Join-Path $testsDir "backend-junit.xml"),
        "--cov=app", "--cov-report", "xml:$(Join-Path $testsDir 'coverage.xml')",
        "--cov-report", "term-missing:skip-covered"
    ))
Check-PytestCompleteness "tests_backend" (Join-Path $testsDir "backend-junit.xml")

Write-Section "7/13  Tests de seguridad"
Register-Step "tests_seguridad" (Invoke-MatrixPython -Arguments @(
        "-m", "pytest", "tests/security", "-q", "--junitxml", (Join-Path $testsDir "security-junit.xml")
    ))
Check-PytestCompleteness "tests_seguridad" (Join-Path $testsDir "security-junit.xml")

Write-Section "8/13  Golden set del RAG"
$ragArgs = @("-m", "scripts.rag_eval", "--output", (Join-Path $testsDir "rag_eval.json"))
$ragArgs += if ($QuickRag) { "--retrieval" } else { "--full" }
Register-Step "rag_golden_set" (Invoke-MatrixPython -Arguments $ragArgs)
if ($QuickRag -and $results["rag_golden_set"].status -eq "PASS") {
    $results["rag_golden_set"].status = "INCOMPLETE"
    $results["rag_golden_set"].detail = "Solo recuperacion; generacion con modelos reales no evaluada"
}

Write-Section "9/13  Playwright E2E"
if ($SkipE2E -or -not $frontendTrusted) {
    $results["e2e"] = [ordered]@{ status = "SKIPPED"; exit_code = 0; detail = "omitido por parametro o cadena de suministro no aprobada" }
    Write-Warn "E2E omitido por parametro o cadena de suministro no aprobada."
}
else {
    & (Join-Path $PSScriptRoot "Start-MatrixRH.ps1") -NoBrowser -SkipPreflight | Out-Null
    if (-not (Test-MatrixBackendUp -TimeoutSec 10)) {
        Register-Step "e2e" 1 "el backend no arranco para los E2E"
    }
    else {
        $corepack = Get-Command corepack -ErrorAction SilentlyContinue
        if (-not $corepack) {
            $results["e2e"] = [ordered]@{ status = "SKIPPED"; exit_code = 0; detail = "corepack no disponible" }
            Write-Warn "corepack no disponible: E2E omitido."
        }
        else {
            Push-Location (Join-Path $root "frontend")
            try {
                & $corepack.Source npm exec --no -- playwright test
                Register-Step "e2e" $LASTEXITCODE
            }
            finally { Pop-Location }
        }
        & (Join-Path $PSScriptRoot "Stop-MatrixRH.ps1") | Out-Null
    }
}

Write-Section "10-11/13  Escenarios Matrix y MatrixR1"
Register-Step "escenarios_identidad" (Invoke-MatrixPython -Arguments @("-m", "scripts.smoke_authorization"))

Write-Section "12/13  Escaneo de secretos y cadena de suministro"
Register-Step "secrets_scan" (Invoke-MatrixPython -Arguments @(
        "-m", "scripts.secrets_scan", "--output", (Join-Path $reportsDir "security\secrets_scan.json")
    ))

Write-Section "13/13  Resumen"
$failed = @($results.Keys | Where-Object { $results[$_].status -eq "FAIL" })
$incomplete = @($results.Keys | Where-Object { $results[$_].status -in @("SKIPPED", "INCOMPLETE") })
$payload = [ordered]@{
    project          = "Matrix RH"
    started_at       = $startedAt.ToString("o")
    finished_at      = (Get-Date).ToString("o")
    host             = $env:COMPUTERNAME
    steps            = $results
    failed_steps     = $failed
    incomplete_steps = $incomplete
    overall          = if ($failed.Count -gt 0) { "FAIL" } elseif ($incomplete.Count -gt 0) { "INCOMPLETE" } else { "PASS" }
}
$outFile = Join-Path $reportsDir "final-validation.json"
$payload | ConvertTo-Json -Depth 6 | Set-Content -Path $outFile -Encoding UTF8

Write-Host ""
Write-Host "  Evidencia: $outFile" -ForegroundColor Cyan
foreach ($key in $results.Keys) {
    $color = if ($results[$key].status -eq "PASS") { "Green" } elseif ($results[$key].status -in @("SKIPPED", "INCOMPLETE")) { "Yellow" } else { "Red" }
    Write-Host ("  {0,-24} {1}" -f $key, $results[$key].status) -ForegroundColor $color
}
Write-Host ""

if ($failed.Count -gt 0) {
    Write-Fail "VALIDACION CON FALLOS: $($failed -join ', ')"
    exit 1
}
if ($incomplete.Count -gt 0) {
    Write-Warn "VALIDACION INCOMPLETA: $($incomplete -join ', '). No certifica la entrega."
    exit 2
}
Write-Ok "VALIDACION COMPLETA APROBADA"
exit 0
