# Creado por Aldo Garcia.
param([Parameter(Mandatory=$true)][string]$Controller)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
$tokens=$null; $errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($Controller,[ref]$tokens,[ref]$errors)
if ($errors.Count) { throw 'AST parse failed' }
foreach ($function in $ast.FindAll({ param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] },$false)) { Invoke-Expression $function.Extent.Text }
$script:Root=Join-Path ([IO.Path]::GetTempPath()) ('matrix incomplete ' + [guid]::NewGuid().ToString('N'))
$script:Runtime=Join-Path $script:Root 'backend/runtime'
$script:Run=Join-Path $script:Root 'knowledge-base/state/run'
$script:Logs=Join-Path $script:Root 'backend/logs'
$script:EnvPath=Join-Path $script:Root 'backend/config/.env'
$script:Python=Join-Path $script:Runtime 'venv/Scripts/python.exe'
$script:Utf8=[Text.UTF8Encoding]::new($false)
$script:CheckFailures=0
$script:Settings=@{OLLAMA_BASE_URL='http://127.0.0.1:11434'}
$script:Independent=[Collections.Generic.List[string]]::new()
function Assert([bool]$Condition,[string]$Message) { if (-not $Condition) { throw $Message } }
function Read-Settings { }
function Assert-LocalConfiguration { $script:Independent.Add('configuration') }
function Assert-Release { $script:Independent.Add('integrity') }
function Assert-InstalledModels { $script:Independent.Add('ollama') }
function Get-Wamp { return $script:Root }
function Find-Apache([string]$Wamp) { $script:Independent.Add('wamp'); return @{service='synthetic'} }
function Invoke-RestMethod { return [pscustomobject]@{models=@()} }
function Invoke-Python { throw 'A blocked Python dependency must not be executed' }
function Wait-Http { throw 'Dependent healthchecks must not wait or fabricate independent failures' }
function Owned-Process { throw 'An uninstalled service must not be probed' }
function MySql-Client { throw 'An uninstalled database must not be contacted' }
try {
    foreach ($dir in @($script:Runtime,$script:Run,$script:Logs)) { Ensure-Directory $dir }
    Write-Text $script:EnvPath 'SYNTHETIC CONFIGURATION'
    Set-InstallStage 'runtime_mysql' 'failed'
    $record=[IO.File]::ReadAllText((Join-Path $script:Run 'installation-progress.json')) | ConvertFrom-Json
    Assert ($record.stage -eq 'runtime_mysql' -and $record.status -eq 'failed') 'Interrupted stage was not persisted'
    $script:DiagnosticFailure=''
    $output=& { try { Diagnose } catch { $script:DiagnosticFailure=$_.Exception.Message } } 6>&1
    Assert ($script:DiagnosticFailure -match 'Diagnostico con fallos') 'Incomplete install must fail with the diagnostic summary'
    Assert ($script:CheckFailures -eq 1) 'Incomplete prerequisites should produce one causal failure, not cascading health failures'
    Assert (($script:Independent -join ',') -eq 'configuration,integrity,ollama,wamp') 'Independent checks must remain available'
    $text=$output | Out-String
    Assert ($text -match '\[PENDIENTE\]' -and $text -match 'runtime_mysql') 'Summary must explain the blocked dependencies and last stage'
    Write-Text $script:Python 'INERT PYTHON'
    foreach ($relative in @('mysql/bin/mysqld.exe','mysql/bin/mysqladmin.exe','mysql/.matrix-package.json')) { Write-Text (Join-Path $script:Runtime $relative) 'INERT RUNTIME' }
    $script:CheckFailures=0
    Set-InstallStage 'runtime_python' 'failed'
    Assert (-not (Test-InstalledRuntimes)) 'Partial uv sync with python.exe present must remain incomplete'
    Assert ($script:CheckFailures -eq 1) 'Failed stage should yield one incomplete-installation finding'
    Set-InstallStage 'completada' 'completed'
    Assert (Test-InstalledRuntimes) 'Completed preparation should enable runtime diagnostics'
    Remove-Item -LiteralPath $script:EnvPath -Force
    $script:CheckFailures=0; $script:Independent.Clear()
    try { Diagnose } catch { Assert ($_.Exception.Message -match 'Diagnostico con fallos') 'Missing config must produce a diagnostic summary' }
    Assert ($script:CheckFailures -eq 1) 'Missing configuration should not cascade'
    Assert (($script:Independent -join ',') -eq 'integrity,wamp') 'Missing config must still permit independent integrity and WAMP checks'
    Write-Host 'SYNTHETIC PASS: incomplete installation, one causal failure, no dependent probes and independent Ollama/WAMP/integrity checks'
} finally { if (Test-Path $script:Root) { Remove-Item -LiteralPath $script:Root -Recurse -Force } }
