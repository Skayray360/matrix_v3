# Creado por Aldo Garcia.
param([Parameter(Mandatory=$true)][string]$Controller)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
$tokens=$null; $errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($Controller,[ref]$tokens,[ref]$errors)
if ($errors.Count) { throw 'AST parse failed' }
foreach ($function in $ast.FindAll({ param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] },$false)) { Invoke-Expression $function.Extent.Text }
Add-Type -AssemblyName System.IO.Compression.FileSystem
$script:TestRoot=Join-Path ([IO.Path]::GetTempPath()) ('matrix mysql synthetic ' + [guid]::NewGuid().ToString('N'))
$script:Utf8=[Text.UTF8Encoding]::new($false)
$script:BinaryVersion='8.4.11'
function Assert([bool]$Condition,[string]$Message) { if (-not $Condition) { throw $Message } }
function Reject([scriptblock]$Body,[string]$Expected) {
    $message=''
    try { & $Body | Out-Null } catch { $message=$_.Exception.Message }
    Assert ($message -match $Expected) ('Unexpected rejection: ' + $message)
}
# Version probes are inert: the synthetic EXEs below are text and never executed.
function MySql-Version([string]$Executable) { $script:Probes++; return $script:BinaryVersion }
function Start-Owned { throw 'Provisioning must never initialize or start a database' }
function New-Case([string]$Name,[bool]$IncludeClient=$true,[string]$ArchiveRoot='mysql-8.4.11-winx64') {
    $script:Root=Join-Path $script:TestRoot $Name
    $script:Config=Join-Path $script:Root 'backend/config'
    $script:Runtime=Join-Path $script:Root 'backend/runtime'
    $script:Run=Join-Path $script:Root 'knowledge-base/state/run'
    $script:Secrets=Join-Path $script:Config 'secrets'
    $script:Probes=0; $script:BinaryVersion='8.4.11'
    foreach ($dir in @($script:Config,$script:Runtime,$script:Run,$script:Secrets)) { Ensure-Directory $dir }
    $archive=Join-Path $script:Runtime 'downloads/mysql-8.4.11-winx64.zip'
    Ensure-Directory ([IO.Path]::GetDirectoryName($archive))
    $source=Join-Path $script:TestRoot ($Name + '-zip-source')
    $server=Join-Path $source ($ArchiveRoot + '/bin/mysqld.exe')
    $client=Join-Path $source ($ArchiveRoot + '/bin/mysqladmin.exe')
    Write-Text $server 'INERT SERVER FIXTURE'
    Write-Text $client 'INERT CLIENT FIXTURE'
    $binaryHashes=@{'mysqld.exe'=(Get-FileHash $server -Algorithm SHA256).Hash.ToLowerInvariant();'mysqladmin.exe'=(Get-FileHash $client -Algorithm SHA256).Hash.ToLowerInvariant()}
    if (-not $IncludeClient) { Remove-Item -LiteralPath $client }
    Write-Text (Join-Path $source ($ArchiveRoot + '/LICENSE')) 'INERT LICENSE FIXTURE'
    [IO.Compression.ZipFile]::CreateFromDirectory($source,$archive)
    $script:Pin=@{version='8.4.11';archive_name='mysql-8.4.11-winx64.zip';archive_root='mysql-8.4.11-winx64';url='https://cdn.mysql.com/Downloads/MySQL-8.4/mysql-8.4.11-winx64.zip';sha256=(Get-FileHash $archive -Algorithm SHA256).Hash.ToLowerInvariant();binary_sha256=$binaryHashes}
    Write-Text (Join-Path $script:Config 'runtime-manifest.json') (@{mysql=$script:Pin} | ConvertTo-Json -Depth 5)
}
try {
    New-Case 'no-wamp-mysql'
    Install-MySql
    $installed=Join-Path $script:Runtime 'mysql'
    Assert (Test-Path (Join-Path $installed 'bin/mysqld.exe')) 'Server was not published'
    Assert (Test-Path (Join-Path $installed 'bin/mysqladmin.exe')) 'Client was not published'
    Assert (Test-Path (Join-Path $installed 'LICENSE')) 'Vendor license was lost'
    Assert (-not (Test-Path (Join-Path $script:Root 'knowledge-base/state/mysql'))) 'Provisioning touched the datadir'
    Assert ($script:Probes -eq 2) 'Both binaries must be probed before publication'
    $receiptPath=Join-Path $installed '.matrix-package.json'
    $receipt=[IO.File]::ReadAllText($receiptPath) | ConvertFrom-Json
    Assert ($receipt.source -eq 'official_mysql_zip') 'Official provenance missing'
    # Idempotence: remove the cached archive and replace download with a failure.
    # Reinstall with older 8.0 must keep its installed runtime and user state.
    Remove-Item -LiteralPath (Join-Path $script:Runtime 'downloads') -Recurse
    function Download-Pinned { throw 'REINSTALL MUST NOT DOWNLOAD OR UPGRADE MYSQL' }
    $script:BinaryVersion='8.0.45'
    $receipt.version='mysqld Ver 8.0.45 for Win64'; $receipt.PSObject.Properties.Remove('mysqladmin_sha256')
    Write-Text $receiptPath ($receipt | ConvertTo-Json)
    $data=Join-Path $script:Root 'knowledge-base/state/mysql/mysql.ibd'
    Write-Text $data 'PRESERVE SYNTHETIC DATABASE'
    $before=[IO.File]::ReadAllText($receiptPath)
    Install-MySql
    Assert ([IO.File]::ReadAllText($receiptPath) -eq $before) 'Existing receipt was replaced'
    Assert ([IO.File]::ReadAllText($data) -eq 'PRESERVE SYNTHETIC DATABASE') 'Existing database changed'
    Write-Text (Join-Path $installed 'bin/mysqld.exe') 'TAMPERED FIXTURE'
    Reject { Install-MySql } 'Cambio no esperado'
    # Restore only the genuine download function: all subsequent downloads use
    # checksummed local fixtures, never the network.
    $definition=$ast.Find({ param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Download-Pinned' },$false)
    Invoke-Expression $definition.Extent.Text
    New-Case 'missing-client' $false
    Reject { Install-MySql } 'ejecutable MySQL no coincide'
    Assert (-not (Test-Path (Join-Path $script:Runtime 'mysql'))) 'Incomplete package became installed'
    New-Case 'wrong-root' $true 'unexpected-root'
    Reject { Install-MySql } 'estructura verificada'
    Assert ($script:Probes -eq 0) 'Wrong archive root reached execution'
    New-Case 'wrong-binary'
    $script:Pin.binary_sha256.'mysqld.exe'='0'*64
    Write-Text (Join-Path $script:Config 'runtime-manifest.json') (@{mysql=$script:Pin} | ConvertTo-Json -Depth 5)
    Reject { Install-MySql } 'ejecutable MySQL no coincide'
    Assert ($script:Probes -eq 0) 'A binary failing hash verification was executed'
    New-Case 'wrong-version'
    $script:BinaryVersion='8.4.9'
    Reject { Install-MySql } 'version del ejecutable'
    Assert (-not (Test-Path (Join-Path $script:Runtime 'mysql'))) 'Wrong version was published'
    New-Case 'lost-runtime'
    Write-Text (Join-Path $script:Root 'knowledge-base/state/mysql/mysql.ibd') 'KEEP DATA'
    Reject { Install-MySql } 'Hay datos MySQL pero falta su runtime'
    Assert ($script:Probes -eq 0) 'Missing runtime with old data attempted to execute a replacement'
    New-Case 'lost-runtime-and-data'
    Write-Text (Join-Path $script:Config 'mysql-initialized.json') '{}'
    Reject { Install-MySql } 'Hay datos MySQL pero falta su runtime'
    New-Case 'interrupted-download'
    function Download-Pinned { throw 'synthetic download or checksum failure' }
    Reject { Install-MySql } 'synthetic download or checksum failure'
    Assert (-not (Test-Path (Join-Path $script:Runtime 'mysql'))) 'Failed download published a runtime'
    Assert ($script:Probes -eq 0) 'Failed download reached binary execution'
    # Timeout recovery: an already live, now healthy owned server must finalize
    # bootstrap and delete its temporary SQL without starting anything again.
    New-Case 'late-healthy'
    Ensure-Directory (Join-Path $script:Root 'knowledge-base/state/mysql/mysql')
    $script:Settings=@{MATRIX_MYSQL_PORT='3308'}
    $script:Healthy=$false
    function Owned-Process([string]$Name) { return [pscustomobject]@{ProcessId=321} }
    function MySql-Client([string]$Command) { Assert ($Command -eq 'status') 'Only authenticated status is permitted'; return $script:Healthy }
    Write-Text (Join-Path $script:Secrets 'mysql-init.sql') 'SYNTHETIC PRIVATE BOOTSTRAP'
    Write-Text (Join-Path $script:Run 'mysql-needs-bootstrap') 'pending'
    Reject { Start-MySql -AllowInitialize } 'bootstrap pendiente'
    Assert (Test-Path (Join-Path $script:Secrets 'mysql-init.sql')) 'Unhealthy server lost retry information'
    $script:Healthy=$true
    Start-MySql -AllowInitialize
    Assert (Test-Path (Join-Path $script:Config 'mysql-initialized.json')) 'Late healthy server needs durable receipt'
    Assert (-not (Test-Path (Join-Path $script:Secrets 'mysql-init.sql'))) 'Late healthy server left bootstrap credentials'
    Assert (-not (Test-Path (Join-Path $script:Run 'mysql-needs-bootstrap'))) 'Late healthy server will repeat bootstrap'
    Write-Host 'SYNTHETIC PASS: independent MySQL ZIP, checksums, layout, idempotence, no automatic upgrade, missing-runtime conservation and late bootstrap cleanup'
} finally { if (Test-Path $script:TestRoot) { Remove-Item -LiteralPath $script:TestRoot -Recurse -Force } }
