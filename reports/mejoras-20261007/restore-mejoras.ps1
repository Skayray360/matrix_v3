# Creado por Aldo Garcia.
[CmdletBinding()]
param([switch]$Apply)
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$rootPrefix = $projectRoot.TrimEnd('\') + '\'
$backupRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'before'))
$backupPrefix = $backupRoot.TrimEnd('\') + '\'

function Assert-NoReparsePath([string]$Path, [string]$Boundary) {
    $cursor = $Path
    while ($true) {
        $item = Get-Item -LiteralPath $cursor -Force
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "No se restauran archivos ni carpetas enlazados: $cursor"
        }
        if ($cursor.Equals($Boundary, [StringComparison]::OrdinalIgnoreCase)) { break }
        $parent = Split-Path -Path $cursor -Parent
        if (-not $parent -or $parent -eq $cursor) { throw 'No se encontro el limite de restauracion' }
        $cursor = $parent
    }
}

$changes = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'changes.json') -Raw | ConvertFrom-Json
$actions = @()
$seen = @{}
foreach ($change in $changes.files) {
    $relative = [string]$change.path
    if ($relative -notmatch '^(README\.md$|\.env\.example$|backend/(app|scripts|tests)/|docs/|SHA256SUMS\.txt$)' -or
        $relative -match '(^|/)\.{1,2}(/|$)|[\\:]' -or $relative -match '[\x00-\x1f]' -or $relative.EndsWith('/')) {
        throw "Ruta fuera del alcance de restauracion: $relative"
    }
    if ($seen.ContainsKey($relative)) { throw "Ruta duplicada: $relative" }
    $seen[$relative] = $true
    if ($change.after_sha256 -notmatch '^[a-f0-9]{64}$') { throw "Hash actual invalido: $relative" }
    $target = [IO.Path]::GetFullPath((Join-Path $projectRoot $relative))
    $backup = [IO.Path]::GetFullPath((Join-Path $backupRoot $relative))
    if (-not $target.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase) -or
        -not $backup.StartsWith($backupPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'La ruta resuelta sale del proyecto o del respaldo'
    }
    if (-not (Test-Path -LiteralPath $target -PathType Leaf)) { throw "Falta archivo actual: $target" }
    Assert-NoReparsePath -Path $target -Boundary $projectRoot
    if ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant() -ne $change.after_sha256) {
        throw "Cambio posterior detectado; conserve y revise manualmente: $target"
    }
    if ($change.kind -eq 'modified') {
        if ($change.before_sha256 -notmatch '^[a-f0-9]{64}$') { throw "Hash de respaldo invalido: $relative" }
        if (-not (Test-Path -LiteralPath $backup -PathType Leaf)) { throw "Falta respaldo: $backup" }
        Assert-NoReparsePath -Path $backup -Boundary $projectRoot
        if ((Get-FileHash -LiteralPath $backup -Algorithm SHA256).Hash.ToLowerInvariant() -ne $change.before_sha256) {
            throw "El respaldo no coincide: $backup"
        }
    } elseif ($change.kind -ne 'new') { throw 'Tipo de cambio desconocido' }
    $actions += [PSCustomObject]@{ Path=$relative; Kind=$change.kind; Target=$target; Backup=$backup }
}
# Validar el lote completo antes de escribir; -Apply es siempre explicito.
$actions | Select-Object Path,Kind | Format-Table -AutoSize
if (-not $Apply) { Write-Output 'Vista previa. Con Matrix detenido, use -Apply para ejecutar.'; exit 0 }
foreach ($action in $actions) {
    if ($action.Kind -eq 'modified') {
        Copy-Item -LiteralPath $action.Backup -Destination $action.Target
    } else {
        # Un unico archivo enumerado, sin eliminaciones recursivas ni indices/datos.
        Remove-Item -LiteralPath $action.Target
    }
}
Write-Output 'Archivos de estas mejoras restaurados. Datos e indices operativos conservados.'
