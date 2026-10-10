# Creado por Aldo Garcia.
param([Parameter(Mandatory=$true)][string]$Controller)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
$tokens=$null; $errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($Controller,[ref]$tokens,[ref]$errors)
if ($errors.Count) { throw 'AST parse failed' }
foreach ($function in $ast.FindAll({ param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] },$false)) { Invoke-Expression $function.Extent.Text }
$script:Root=Join-Path ([IO.Path]::GetTempPath()) ('matrix guard espa' + [char]0xF1 + 'ol ' + [guid]::NewGuid().ToString('N'))
$script:Config=Join-Path $script:Root 'backend/config'
$script:Secrets=Join-Path $script:Config 'secrets'
$script:Runtime=Join-Path $script:Root 'backend/runtime'
$script:Run=Join-Path $script:Root 'knowledge-base/state/run'
$script:Logs=Join-Path $script:Root 'backend/logs'
$script:Utf8=[Text.UTF8Encoding]::new($false)
function Assert([bool]$Condition,[string]$Message) { if (-not $Condition) { throw $Message } }
function Must-Reject([scriptblock]$Body,[string]$Message) {
    $rejected=$false
    try { & $Body | Out-Null } catch { $rejected=$true }
    Assert $rejected $Message
}
try {
    foreach ($dir in @($script:Config,$script:Secrets,$script:Runtime,$script:Run,$script:Logs)) { Ensure-Directory $dir }
    Assert-OperationalPaths
    $secret=New-Secret 'stable.txt' $false
    Assert ($secret -match '^[0-9a-f]{64}$') 'secret must be a 256-bit hex value'
    Assert ((New-Secret 'stable.txt' $true) -ceq $secret) 'reinstall must not reset an existing secret'
    Must-Reject { New-Secret 'missing.txt' $true } 'existing database must prevent generating replacement secrets'
    Assert (-not (Test-Path (Join-Path $script:Secrets 'missing.txt'))) 'rejected secret created a file'
    Write-Text (Join-Path $script:Secrets 'bad.txt') 'invalid'
    Must-Reject { New-Secret 'bad.txt' $false } 'invalid existing secret must not be overwritten'
    Assert ((Get-Content (Join-Path $script:Secrets 'bad.txt') -Raw) -eq 'invalid') 'invalid secret changed'

    $script:FakeProcess=$null
    function Get-CimInstance { param($ClassName,$Filter,$ErrorAction); return $script:FakeProcess }
    $created=[DateTime]::UtcNow
    $exe=$script:Runtime + '\mysql\bin\mysqld.exe'
    $record=@{pid=42;executable=$exe;created_ticks=$created.Ticks.ToString()}
    Write-Text (Process-Record 'mysql') ($record | ConvertTo-Json)
    $script:FakeProcess=[pscustomobject]@{ProcessId=42;ExecutablePath=$exe;CreationDate=$created}
    Assert ($null -ne (Owned-Process 'mysql')) 'matching process identity was rejected'
    $script:FakeProcess.CreationDate=$created.AddSeconds(1)
    Assert ($null -eq (Owned-Process 'mysql')) 'reused PID must not be adopted'
    $script:FakeProcess.CreationDate=$created
    $script:FakeProcess.ExecutablePath='C:\another-project\mysqld.exe'
    Assert ($null -eq (Owned-Process 'mysql')) 'foreign executable must not be adopted'
    $script:FakeProcess.ExecutablePath=$null
    $identityError=''
    try { Owned-Process 'mysql' | Out-Null } catch { $identityError=$_.Exception.Message }
    Assert ($identityError -match 'Administrador') 'inaccessible process identity needs actionable elevation guidance'

    $epoch=[datetime]::SpecifyKind([datetime]'1970-01-01',[DateTimeKind]::Utc)
    $backend=@{pid=42;root=$script:Root;created_at=($created-$epoch).TotalSeconds}
    $backendPath=Join-Path $script:Run 'matrixrh-backend.identity.json'
    Write-Text $backendPath ($backend | ConvertTo-Json)
    $script:FakeProcess.ExecutablePath=$script:Runtime + '\venv\Scripts\python.exe'
    Assert ($null -ne (Backend-Process)) 'matching backend identity was rejected'
    $script:FakeProcess.CreationDate=$created.AddSeconds(1)
    Assert ($null -eq (Backend-Process)) 'backend PID reuse must not be adopted'
    $script:FakeProcess.CreationDate=$created
    $script:FakeProcess.ExecutablePath='C:\another-project\python.exe'
    Assert ($null -eq (Backend-Process)) 'foreign backend executable must not be adopted'
    $script:FakeProcess.ExecutablePath=$null
    $identityError=''
    try { Backend-Process | Out-Null } catch { $identityError=$_.Exception.Message }
    Assert ($identityError -match 'Administrador') 'backend inaccessible identity needs elevation guidance'
    $script:FakeProcess.ExecutablePath=$script:Runtime + '\venv\Scripts\python.exe'
    $backend.root=$script:Root + '-another'
    Write-Text $backendPath ($backend | ConvertTo-Json)
    Must-Reject { Backend-Process } 'active backend identity from another project must be rejected'
    $script:FakeProcess=$null
    Assert ($null -eq (Backend-Process)) 'dead identity must not prevent a new installation'

    # Test release verification with inert files; none is executed or imported.
    $required=@('backend/app/__init__.py','backend/app/main.py','backend/pyproject.toml','backend/uv.lock','backend/scripts/windows/MatrixRH.ps1','backend/scripts/windows/launch_process.py','backend/config/runtime-manifest.json','backend/config/apache/matrix-rh.conf.template','.htaccess','instalar.bat','iniciar.bat','detener.bat','diagnosticar.bat')
    foreach ($relative in $required) { Write-Text (Join-Path $script:Root $relative) 'inert release fixture' }
    Write-Text (Join-Path $script:Root 'backend/app/__init__.py') '__version__ = "1.4.0"'
    Write-Text (Join-Path $script:Root 'backend/pyproject.toml') 'version = "1.4.0"'
    foreach ($relative in @('backend/seeds','backend/migrations')) { Ensure-Directory (Join-Path $script:Root $relative) }
    $manifest=@($required | ForEach-Object { (Get-FileHash (Join-Path $script:Root $_) -Algorithm SHA256).Hash.ToLowerInvariant() + '  ' + $_ })
    Write-Text (Join-Path $script:Root 'backend/release/SHA256SUMS.txt') ([string]::Join([Environment]::NewLine,$manifest) + [Environment]::NewLine)
    Assert-Release
    Write-Text (Join-Path $script:Root 'backend/app/__init__.py') '__version__ = "crossed-package"'
    Must-Reject { Assert-Release } 'a crossed release must fail before installation'
    Assert ((Get-Content (Join-Path $script:Secrets 'stable.txt') -Raw) -eq $secret) 'release check mutated credentials'

    # Simulate a junction attribute portably. No real link, target or service is
    # created; all other Get-Item calls use the actual filesystem provider.
    $script:JunctionPath=Join-Path $script:Root 'knowledge-base/state/mysql'
    Ensure-Directory $script:JunctionPath
    function Get-Item {
        param([string]$LiteralPath,[switch]$Force)
        if ([IO.Path]::GetFullPath($LiteralPath) -eq [IO.Path]::GetFullPath($script:JunctionPath)) { return [pscustomobject]@{Attributes=[IO.FileAttributes]::Directory -bor [IO.FileAttributes]::ReparsePoint} }
        return Microsoft.PowerShell.Management\Get-Item -LiteralPath $LiteralPath -Force:$Force
    }
    Must-Reject { Assert-OperationalPaths } 'junction datadir must fail before runtime creation'
    Must-Reject { Ensure-Directory (Join-Path $script:JunctionPath 'new-child') } 'existing junction ancestor must block directory writes'
    Assert (-not (Test-Path (Join-Path $script:JunctionPath 'new-child'))) 'guard wrote through redirected datadir'
    Remove-Item Function:Get-Item

    # The external executable is replaced only inside this probe's scope.
    # Its stderr mirrors a PowerShell 5.1 NativeCommandError during startup.
    function Test-NativeProbe {
        function Join-Path { param($Path,$ChildPath); if ($ChildPath -like '*mysqladmin.exe') { return 'mysqladmin-probe' }; return 'private-options.ini' }
        $script:ProbeReady=$false
        function mysqladmin-probe {
            Assert ($args[0] -eq '--defaults-file=private-options.ini') 'client must read only the private config'
            Assert ($args[1] -eq '--no-login-paths') 'client must ignore user login files'
            Assert ($ErrorActionPreference -eq 'Continue') 'native stderr must not terminate the readiness loop on PowerShell 5.1'
            if ($script:ProbeReady) { $global:LASTEXITCODE=0 }
            else { Write-Error 'expected synthetic connection refused'; $global:LASTEXITCODE=1 }
        }
        Assert (-not (MySql-Client 'status')) 'first refused connection must return false'
        Assert ($ErrorActionPreference -eq 'Stop') 'native probe leaked its error preference'
        $script:ProbeReady=$true
        Assert (MySql-Client 'status') 'authenticated ready probe must return true'
        Assert ($ErrorActionPreference -eq 'Stop') 'successful probe leaked its error preference'
    }
    Test-NativeProbe
    Write-Host 'SYNTHETIC PASS: secret preservation, release integrity, PID ownership, inaccessible identities, junction rejection and PowerShell 5.1 native stderr retry'
} finally { if (Test-Path $script:Root) { Remove-Item $script:Root -Recurse -Force } }
