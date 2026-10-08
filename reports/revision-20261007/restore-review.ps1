[CmdletBinding()]
param([switch]$Apply)
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$rootPrefix = $projectRoot.TrimEnd('\') + '\'
$backupRoot = Join-Path $PSScriptRoot 'before'
$changes = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'changes.json') -Raw | ConvertFrom-Json
$actions = @()
foreach ($change in $changes.files) {
    if ($change.path -notmatch '^(backend/app/|backend/tests/unit/|docs/|SHA256SUMS\.txt$)') {
        throw "Ruta fuera del alcance de restauracion: $($change.path)"
    }
    $target = [IO.Path]::GetFullPath((Join-Path $projectRoot $change.path))
    if (-not $target.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'La ruta resuelta sale del proyecto'
    }
    if (-not (Test-Path -LiteralPath $target -PathType Leaf)) { throw "Falta archivo actual: $target" }
    $item = Get-Item -LiteralPath $target -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "No se restauran enlaces: $target" }
    if ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant() -ne $change.after_sha256) {
        throw "Cambio posterior detectado; conserve y revise manualmente: $target"
    }
    $backup = Join-Path $backupRoot $change.path
    if ($change.kind -eq 'modified') {
        if (-not (Test-Path -LiteralPath $backup -PathType Leaf)) { throw "Falta respaldo: $backup" }
        if ((Get-FileHash -LiteralPath $backup -Algorithm SHA256).Hash.ToLowerInvariant() -ne $change.before_sha256) {
            throw "El respaldo no coincide: $backup"
        }
    } elseif ($change.kind -ne 'new') { throw 'Tipo de cambio desconocido' }
    $actions += [PSCustomObject]@{ Path=$change.path; Kind=$change.kind; Target=$target; Backup=$backup }
}
# Comprobar TODO antes de escribir; nunca restaurar una parte tras detectar otro cambio.
$actions | Select-Object Path,Kind | Format-Table -AutoSize
if (-not $Apply) { Write-Output 'Vista previa. Con Matrix detenido, use -Apply para ejecutar.'; exit 0 }
foreach ($action in $actions) {
    if ($action.Kind -eq 'modified') {
        Copy-Item -LiteralPath $action.Backup -Destination $action.Target
    } else {
        # Eliminacion de un unico archivo enumerado y verificado; nunca recursiva.
        Remove-Item -LiteralPath $action.Target
    }
}
Write-Output 'Archivos de esta revision restaurados. Los datos operativos no se modificaron.'
