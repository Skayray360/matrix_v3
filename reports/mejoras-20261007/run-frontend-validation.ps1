param([ValidateSet('baseline', 'after')][string]$Phase = 'after')
$ErrorActionPreference = 'Stop'
$reportRoot = $PSScriptRoot
$projectRoot = Split-Path (Split-Path $reportRoot -Parent) -Parent
$inputRoot = if ($Phase -eq 'baseline') { Join-Path $reportRoot 'before/frontend' } else { Join-Path $projectRoot 'frontend' }
$dependencies = Join-Path $projectRoot 'reports/review-20261006-1649/frontend-check/node_modules'
$dependencyLock = Join-Path (Split-Path $dependencies -Parent) 'package-lock.json'
if ((Get-FileHash -LiteralPath (Join-Path $inputRoot 'package-lock.json')).Hash -ne (Get-FileHash -LiteralPath $dependencyLock).Hash) {
    throw 'Offline dependency lock does not match the selected frontend snapshot'
}
$runRoot = Join-Path $reportRoot ('frontend-' + $Phase + '-' + [DateTimeOffset]::Now.ToUnixTimeMilliseconds())
New-Item -ItemType Directory -Path $runRoot | Out-Null
Get-ChildItem -LiteralPath $inputRoot -Force | Where-Object { $_.Name -notin @('node_modules', 'dist', 'tsconfig.tsbuildinfo', '.env', '.env.local') } | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $runRoot -Recurse
}
New-Item -ItemType Junction -Path (Join-Path $runRoot 'node_modules') -Target $dependencies | Out-Null
$previousOptions = $env:NODE_OPTIONS
$env:NODE_OPTIONS = '--require="' + (Join-Path $reportRoot 'frontend-offline.cjs').Replace('\', '/') + '"'
$commands = @(
    @{Name='test'; Args=@('test', '--', '--maxWorkers=2', '--no-file-parallelism', '--reporter=json', '--outputFile=test-results.json')},
    @{Name='typecheck'; Args=@('run', 'typecheck')},
    @{Name='lint'; Args=@('run', 'lint')},
    @{Name='build'; Args=@('run', 'build')}
)
$results = @()
Push-Location $runRoot
try {
    foreach ($command in $commands) {
        $started = Get-Date
        $commandArgs = $command.Args
        # Windows PowerShell 5 wraps native stderr as ErrorRecord. Preserve it
        # in the log and use the process exit code rather than aborting early.
        $ErrorActionPreference = 'Continue'
        & npm.cmd @commandArgs *> ($command.Name + '.log')
        $ErrorActionPreference = 'Stop'
        $results += @{name=$command.Name; command=('npm.cmd ' + ($commandArgs -join ' ')); exit_code=$LASTEXITCODE; seconds=((Get-Date) - $started).TotalSeconds}
    }
}
finally {
    Pop-Location
    $env:NODE_OPTIONS = $previousOptions
}
$summary = @{phase=$Phase; source=$inputRoot; run=$runRoot; node=(& node --version); lock_sha256=(Get-FileHash -LiteralPath $dependencyLock).Hash; network='External Node TCP connections and all UDP sends blocked; Vitest IPC/loopback allowed; component tests mock API'; results=$results}
$summary | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $runRoot 'validation-results.json') -Encoding utf8
$summary | ConvertTo-Json -Depth 5
