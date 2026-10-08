# Restauracion selectiva opcional. Este script NO se ejecuto durante la revision.
# Ejecutar con Matrix detenido; rechaza cambios posteriores no respaldados.
$ErrorActionPreference = 'Stop'
$reviewRoot = $PSScriptRoot
$projectRoot = [IO.Path]::GetFullPath((Join-Path $reviewRoot '../..'))
$projectPrefix = $projectRoot.TrimEnd('\') + '\'
$changes = @(Get-Content -LiteralPath (Join-Path $reviewRoot 'changed-files.json') -Raw | ConvertFrom-Json)

# Verificar todo antes de restaurar el primer archivo.
foreach ($entry in $changes) {
    $target = [IO.Path]::GetFullPath((Join-Path $projectRoot $entry.path))
    if (-not $target.StartsWith($projectPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Ruta fuera del proyecto: $($entry.path)"
    }
    if (-not (Test-Path -LiteralPath $target -PathType Leaf)) { throw "Archivo ausente: $target" }
    if ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant() -ne $entry.after_sha256) {
        throw "Hay cambios posteriores: $($entry.path). Revisar antes de restaurar."
    }
    if (-not $entry.created) {
        $backupPath = Join-Path $reviewRoot $entry.backup
        if ((Get-FileHash -LiteralPath $backupPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $entry.before_sha256) {
            throw "Respaldo no coincide: $($entry.path)"
        }
    }
}

foreach ($entry in $changes | Where-Object { $_.path -ne 'SHA256SUMS.txt' }) {
    $target = [IO.Path]::GetFullPath((Join-Path $projectRoot $entry.path))
    if ($entry.created) {
        Remove-Item -LiteralPath $target
    } else {
        Copy-Item -LiteralPath (Join-Path $reviewRoot $entry.backup) -Destination $target
    }
}
Copy-Item -LiteralPath (Join-Path $reviewRoot 'backup/SHA256SUMS.txt') -Destination (Join-Path $projectRoot 'SHA256SUMS.txt')
Write-Output 'Restaurados solo los archivos de esta revision; datos y trabajo previo conservados.'
