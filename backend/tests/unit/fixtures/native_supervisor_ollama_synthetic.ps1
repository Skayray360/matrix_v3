# Creado por Aldo Garcia.
param([Parameter(Mandatory=$true)][string]$Controller)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
$tokens=$null; $errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($Controller,[ref]$tokens,[ref]$errors)
if ($errors.Count) { throw 'AST parse failed' }
foreach ($function in $ast.FindAll({ param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] },$false)) { Invoke-Expression $function.Extent.Text }
function Assert([bool]$Condition,[string]$Message) { if (-not $Condition) { throw $Message } }
$script:Settings=@{OLLAMA_BASE_URL='http://127.0.0.1:11434';OLLAMA_FAST_MODEL='gemma4:latest';OLLAMA_DEEP_MODEL='gemma4:latest';OLLAMA_EMBEDDING_MODEL='embeddinggemma:latest'}
$script:EnvPath='synthetic-env-path'
$script:Requests=[Collections.Generic.List[string]]::new()
$script:Models=@([pscustomobject]@{name='gemma4:latest';digest=('a'*64)},[pscustomobject]@{name='embeddinggemma:latest';digest=('b'*64)})
$script:Offline=$false
$script:Malformed=$false
function Invoke-RestMethod([string]$Uri,[int]$TimeoutSec,[int]$MaximumRedirection) {
    Assert ($Uri -eq 'http://127.0.0.1:11434/api/tags') 'Unexpected API request: only inventory is allowed'
    Assert ($MaximumRedirection -eq 0) 'Inventory must not follow redirects'
    Assert ($TimeoutSec -le 10) 'Inventory must use a finite timeout'
    $script:Requests.Add($Uri)
    if ($script:Offline) { throw 'synthetic unreachable service' }
    if ($script:Malformed) { return [pscustomobject]@{service='not Ollama'} }
    return [pscustomobject]@{models=$script:Models}
}
function Start-Owned { throw 'Matrix must not start another Ollama instance' }
function Start-Process { throw 'Matrix must not execute the installed Ollama CLI' }
function Stop-Process { throw 'Matrix must not kill the external Ollama process' }
function Invoke-Checked { throw 'No executable or model download is authorized by inventory checks' }
Assert-InstalledModels
Assert ($script:Requests.Count -eq 1) 'Existing models must be reused with one inventory request'
# Partial inventory: fail clearly, without any pull or replacement call.
$script:Models=@($script:Models[0])
$errorText=''
try { Assert-InstalledModels } catch { $errorText=$_.Exception.Message }
Assert ($errorText -match 'Falta el modelo embeddinggemma:latest') 'Missing embedding model must be named'
Assert ($script:Requests.Count -eq 2) 'Missing model must not trigger a download request'
$script:Models=@()
$errorText=''
try { Assert-InstalledModels } catch { $errorText=$_.Exception.Message }
Assert ($errorText -match 'Falta el modelo gemma4:latest') 'Empty inventory must remain actionable'
$script:Malformed=$true
$errorText=''
try { Assert-InstalledModels } catch { $errorText=$_.Exception.Message }
Assert ($errorText -match 'inventario valido') 'Foreign service must not be accepted as Ollama'
$script:Malformed=$false; $script:Offline=$true
$errorText=''
try { Assert-InstalledModels } catch { $errorText=$_.Exception.Message }
Assert ($errorText -match 'Abre Ollama') 'Offline service must request opening the existing Ollama'
$script:Offline=$false
foreach ($url in @('http://remote.invalid:11434','https://127.0.0.1:11434','http://127.0.0.1:11434/other','http://user@127.0.0.1:11434','http://127.0.0.1:11434?x=1')) {
    $script:Settings.OLLAMA_BASE_URL=$url
    $before=$script:Requests.Count
    $blocked=$false
    try { Assert-InstalledModels } catch { $blocked=$true }
    Assert $blocked 'Remote or malformed endpoint accepted'
    Assert ($script:Requests.Count -eq $before) 'Endpoint must be rejected before network access'
}
$script:Settings.OLLAMA_BASE_URL='http://127.0.0.1:11434'
$script:Models=@([pscustomobject]@{name='gemma4:latest';digest=('a'*64);remote_host='remote.invalid'})
$errorText=''
try { Assert-InstalledModels } catch { $errorText=$_.Exception.Message }
Assert ($errorText -match 'es remoto') 'Remote alias must not be used as a local model'
$script:Models=@([pscustomobject]@{name='gemma4:latest';digest='a'*12})
$errorText=''
try { Assert-InstalledModels } catch { $errorText=$_.Exception.Message }
Assert ($errorText -match 'digest invalido') 'Short model IDs must not count as full pins'
$script:Models=@([pscustomobject]@{name='gemma4:latest';digest=('a'*64)},[pscustomobject]@{name='embeddinggemma:latest';digest=('b'*64)})
$script:Commands=[Collections.Generic.List[string]]::new()
function Invoke-Python([string[]]$Arguments) { $script:Commands.Add(($Arguments -join ' ')) }
function Secure-File([string]$Path) { Assert ($Path -eq $script:EnvPath) 'Private model pins must keep file permissions'; $script:Commands.Add('secure-env') }
function Read-Settings { $script:Commands.Add('reload-pins') }
Ensure-Models
Assert (($script:Commands -join '|') -eq '-m scripts.bootstrap pin-models|secure-env|reload-pins|-m scripts.model_smoke_test --run-inference --profile all') 'Install must validate full digests, generation and embeddings with no pull'
# Stop still closes the project's database; it never owns the shared model service.
$script:Events=[Collections.Generic.List[string]]::new()
$script:MySqlRunning=$true
function Assert-LocalConfiguration { }
function Stop-Backend { $script:Events.Add('backend') }
function Owned-Process([string]$Name) {
    Assert ($Name -eq 'mysql') 'External Ollama must not be adopted or inspected as an owned process'
    if ($script:MySqlRunning) { return [pscustomobject]@{ProcessId=123} }
    return $null
}
function MySql-Client([string]$Command) {
    Assert ($Command -eq 'shutdown') 'Stop must gracefully shut down only MySQL'
    $script:Events.Add('mysql'); $script:MySqlRunning=$false; return $true
}
Stop-Stack
Assert (($script:Events -join ',') -eq 'backend,mysql') 'Stopping Matrix must leave Ollama untouched'
Write-Host 'SYNTHETIC PASS: existing Ollama, missing models, malformed API, local-only URL, full digests, inference checks, no downloads and no external service stop'
