# Creado por Aldo Garcia.
param([Parameter(Mandatory=$true)][string]$Controller)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
$file=$Controller
$tokens=$null; $errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($file,[ref]$tokens,[ref]$errors)
if ($errors.Count) { throw 'AST parse failed' }
foreach ($function in $ast.FindAll({ param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] },$false)) { Invoke-Expression $function.Extent.Text }
$script:Root=Join-Path ([IO.Path]::GetTempPath()) ('matrix-synthetic-' + [guid]::NewGuid().ToString('N'))
$script:Config=Join-Path $script:Root 'backend/config'
$script:Secrets=Join-Path $script:Config 'secrets'
$script:Runtime=Join-Path $script:Root 'backend/runtime'
$script:Run=Join-Path $script:Root 'knowledge-base/state/run'
$script:Logs=Join-Path $script:Root 'backend/logs'
$script:EnvPath=Join-Path $script:Config '.env'
$script:Utf8=[Text.UTF8Encoding]::new($false)
$script:Settings=@{}
function Assert([bool]$Condition,[string]$Message) { if(-not $Condition) { throw $Message } }
try {
    foreach($dir in @($script:Config,$script:Secrets,$script:Run,$script:Logs,$script:Runtime)) { Ensure-Directory $dir }
    Write-Text $script:EnvPath ([string]::Join([Environment]::NewLine,@('APP_ENV=standalone','AUTH_PROVIDER=local','APP_HOST=127.0.0.1','APP_PORT=8000','APP_BASE_URL=http://127.0.0.1:8085','OLLAMA_BASE_URL=http://127.0.0.1:11434','MATRIX_MYSQL_PORT=3308','RAG_KNOWLEDGE_ROOT=./knowledge-base/documents','QDRANT_PATH=./knowledge-base/state/qdrant','UPLOAD_STORAGE_ROOT=./knowledge-base/state/uploads','DATABASE_DATADIR_PATH=./knowledge-base/state/mysql','QDRANT_MODE=embedded',('DATABASE_URL=mysql+pymysql://matrixrh:' + ('b'*64) + '@127.0.0.1:3308/matrix_rh?charset=utf8mb4'))))
    Read-Settings
    foreach($name in $script:Settings.Keys) { Remove-Item ('Env:' + $name) -ErrorAction SilentlyContinue }
    Assert-LocalConfiguration
    [Environment]::SetEnvironmentVariable('APP_ENV','production','Process')
    $inheritedBlocked=$false
    try { Assert-LocalConfiguration } catch { $inheritedBlocked=$true }
    Assert $inheritedBlocked 'inherited override must be rejected'
    Remove-Item Env:APP_ENV -ErrorAction SilentlyContinue
    Set-Setting 'TEST_LITERAL' '$1 $& " not shell'
    Read-Settings
    Assert ((Setting 'TEST_LITERAL') -eq '$1 $& " not shell') 'dotenv roundtrip changed literal data'
    Assert ((Quote-Argument 'C:\space root\end\') -eq '"C:\space root\end\\"') 'Windows argument quoting failed'
    Assert ((Quote-Argument 'a"b') -eq '"a\"b"') 'embedded quote handling failed'
    Set-Setting 'MATRIX_MYSQL_PORT' '8000'
    $blocked=$false
    try { Assert-LocalConfiguration } catch { $blocked=$true }
    Assert $blocked 'duplicate ports must be rejected'
    Set-Setting 'MATRIX_MYSQL_PORT' '3308'
    Write-Text (Join-Path $script:Secrets 'mysql-root-password.txt') ('a'*64)
    Write-Text (Join-Path $script:Secrets 'mysql-app-password.txt') ('b'*64)
    $script:Initialized=$false; $script:ServerStarted=$false
    function Owned-Process([string]$Name) { return $null }
    function Port-Free([int]$Port) { return $true }
    function MySql-Client([string]$Command) { Assert ($Command -eq 'status') 'health must authenticate, not mysqladmin ping'; return $script:ServerStarted }
    function Invoke-Checked([string]$Executable,[string[]]$Arguments) {
        Assert ($Arguments -contains '--initialize-insecure') 'unexpected execution'
        Assert ($Arguments -contains '--skip-networking') 'initialize must not expose network'
        Assert (Test-Path (Join-Path $script:Run 'mysql-needs-bootstrap')) 'bootstrap marker must precede initialization'
        $script:Initialized=$true
    }
    function Start-Owned([string]$Name,[string]$Executable,[string[]]$Arguments) {
        Assert ($Name -eq 'mysql') 'unexpected runtime'
        Assert $script:Initialized 'initialization must complete first'
        $sql=Get-Content -LiteralPath (Join-Path $script:Secrets 'mysql-init.sql') -Raw
        Assert ($sql -notmatch 'CREATE DATABASE') 'migrator must create and own schema'
        Assert ($sql -match 'GRANT SHUTDOWN') 'shutdown account grant missing'
        Assert (($Arguments -join ' ') -notmatch 'aaaa|bbbb') 'password on command line'
        $script:ServerStarted=$true
    }
    $missingDataError=''
    try { Start-MySql } catch { $missingDataError=$_.Exception.Message }
    Assert ($missingDataError -match 'restaure un respaldo coherente') 'start with a missing datadir must request restoration'
    Assert (-not $script:Initialized -and -not $script:ServerStarted) 'normal start initialized or bootstrapped missing data'
    Assert (-not (Test-Path (Join-Path $script:Config 'mysql.ini'))) 'missing datadir start must fail before writing MySQL configuration'
    Start-MySql -AllowInitialize
    $ini=Get-Content -LiteralPath (Join-Path $script:Config 'mysql.ini') -Raw
    Assert ($ini -match '(?m)^port=3308$') 'my.ini port must be one complete line'
    Assert ($ini -match '(?m)^basedir=".+/backend/runtime/mysql"$') 'my.ini basedir malformed'
    Assert ($ini -match '(?m)^datadir=".+/knowledge-base/state/mysql"$') 'my.ini datadir malformed'
    Assert (-not (Test-Path (Join-Path $script:Secrets 'mysql-init.sql'))) 'plaintext bootstrap SQL must be removed after authenticated ready'
    Assert (-not (Test-Path (Join-Path $script:Run 'mysql-needs-bootstrap'))) 'completed bootstrap marker not cleared'
    Assert (Test-Path (Join-Path $script:Config 'mysql-initialized.json')) 'authenticated initial startup must persist a receipt outside the datadir'
    # The fake runtime never creates system tables, matching their subsequent
    # loss. Even installing again must not recreate a previously owned database.
    $script:Initialized=$false; $script:ServerStarted=$false
    $missingDataError=''
    try { Start-MySql -AllowInitialize } catch { $missingDataError=$_.Exception.Message }
    Assert ($missingDataError -match 'restaure un respaldo coherente') 'reinstall with a lost datadir must request restoration'
    Assert (-not $script:Initialized -and -not $script:ServerStarted) 'reinstall recreated a database with an existing initialization receipt'
    # Exercise Start-Stack with only mocked services: the conservation guard
    # must run before preflight, readiness reuse or serving, on either branch.
    function Test-StartConservation([bool]$ExistingBackend) {
        $script:StartEvents=[Collections.Generic.List[string]]::new()
        function Assert-Release { $script:StartEvents.Add('integrity') }
        function Assert-LocalConfiguration { $script:StartEvents.Add('configuration') }
        function Start-MySql { param([switch]$AllowInitialize); Assert (-not $AllowInitialize) 'start must not opt into initialization'; $script:StartEvents.Add('mysql') }
        function Assert-InstalledModels { $script:StartEvents.Add('ollama') }
        function Port-Free([int]$Port) { $script:StartEvents.Add('port'); return (-not $ExistingBackend) }
        function Backend-Process { $script:StartEvents.Add('backend'); return @{pid=42} }
        function Wait-Http { throw 'readiness must not bypass a rejected conservation guard' }
        function Invoke-Python([string[]]$Arguments) {
            Assert ($Arguments -contains 'scripts.installation_state' -and $Arguments -contains '--guard-install') 'preflight/serve ran before installation state guard'
            $script:StartEvents.Add('state-guard')
            throw 'synthetic incomplete state'
        }
        $failure=''
        try { Start-Stack } catch { $failure=$_.Exception.Message }
        Assert ($failure -eq 'synthetic incomplete state') 'start did not propagate conservation failure'
        Assert (($script:StartEvents -join ',') -eq 'integrity,configuration,ollama,mysql,state-guard') 'start accessed backend or vector store before its conservation guard'
    }
    Test-StartConservation $false
    Test-StartConservation $true
    Write-Host 'SYNTHETIC PASS: quoting, dotenv, port isolation, explicit MySQL initialization, missing-data conservation, guard before serving, credential isolation, authenticated readiness and cleanup'
} finally { if(Test-Path $script:Root) { Remove-Item $script:Root -Recurse -Force } }
