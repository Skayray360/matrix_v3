# Creado por Aldo Garcia.
param([Parameter(Mandatory=$true)][string]$Controller)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
$source=$Controller
$tokens=$null; $errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($source,[ref]$tokens,[ref]$errors)
if($errors.Count) { throw 'AST parse failed' }
foreach($function in $ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst]},$false)) { Invoke-Expression $function.Extent.Text }
$script:Root=Join-Path ([IO.Path]::GetTempPath()) ('matrix-proxy-synthetic-' + [guid]::NewGuid().ToString('N'))
$script:Config=Join-Path $script:Root 'backend/config'
$script:Run=Join-Path $script:Root 'knowledge-base/state/run'
$script:Utf8=[Text.UTF8Encoding]::new($false)
$script:Settings=@{APP_BASE_URL='http://127.0.0.1:8085';APP_PORT='8000'}
$script:ApacheRoot=Join-Path $script:Root 'fake-wamp-apache'
function Assert([bool]$Condition,[string]$Message) { if(-not $Condition) { throw $Message } }
try {
    Ensure-Directory $script:Run
    Ensure-Directory (Join-Path $script:Config 'apache')
    Ensure-Directory (Join-Path $script:ApacheRoot 'modules')
    Write-Text (Join-Path $script:ApacheRoot 'httpd.conf') 'Original Apache configuration'
    Write-Text (Join-Path $script:ApacheRoot 'modules/mod_proxy.so') 'synthetic'
    Write-Text (Join-Path $script:ApacheRoot 'modules/mod_proxy_http.so') 'synthetic'
    Write-Text (Join-Path $script:Config 'apache/matrix-rh.conf') 'Original proxy configuration'
    Write-Text (Join-Path $script:Config 'apache/matrix-rh.conf.template') 'Listen 127.0.0.1:{{WEB_PORT}}'
    function Find-Apache([string]$Wamp) { return @{base=$script:ApacheRoot;config=(Join-Path $script:ApacheRoot 'httpd.conf');executable='synthetic-httpd';service='synthetic-wamp'} }
    function Invoke-Checked([string]$Executable,[string[]]$Arguments) { throw 'synthetic invalid Apache syntax' }
    $rejected=$false
    try { Configure-Apache 'synthetic-wamp' } catch { $rejected=$true }
    Assert $rejected 'invalid Apache configuration was accepted'
    Assert ((Get-Content (Join-Path $script:ApacheRoot 'httpd.conf') -Raw) -eq 'Original Apache configuration') 'httpd.conf was not restored'
    Assert ((Get-Content (Join-Path $script:Config 'apache/matrix-rh.conf') -Raw) -eq 'Original proxy configuration') 'existing proxy was not restored'
    Assert (@(Get-ChildItem $script:Run -Filter '*.conf.bak').Count -eq 1) 'rollback backup was not retained'
    # Missing module is detected before modifying the proxy.
    Remove-Item (Join-Path $script:ApacheRoot 'modules/mod_proxy.so')
    try { Configure-Apache 'synthetic-wamp' } catch { }
    Assert ((Get-Content (Join-Path $script:Config 'apache/matrix-rh.conf') -Raw) -eq 'Original proxy configuration') 'prerequisite failure changed active proxy'
    Write-Host 'SYNTHETIC PASS: Apache validation failure restores both files and keeps backup; missing module leaves active config unchanged'
} finally { if(Test-Path $script:Root) { Remove-Item $script:Root -Recurse -Force } }
