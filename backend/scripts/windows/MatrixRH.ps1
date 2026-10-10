# Creado por Aldo Garcia.
# Matrix RH: native Windows supervisor. PowerShell 5.1+, no shared database.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][ValidateSet('instalar','iniciar','detener','diagnosticar')][string]$Action,
    [switch]$NoBrowser,
    [string]$WampRoot = ''
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$script:Root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\..'))
$script:Config = Join-Path $script:Root 'backend\config'
$script:Runtime = Join-Path $script:Root 'backend\runtime'
$script:Run = Join-Path $script:Root 'knowledge-base\state\run'
$script:Logs = Join-Path $script:Root 'backend\logs'
$script:Secrets = Join-Path $script:Config 'secrets'
$script:EnvPath = Join-Path $script:Config '.env'
$script:Python = Join-Path $script:Runtime 'venv\Scripts\python.exe'
$script:Utf8 = New-Object Text.UTF8Encoding($false)
$script:Settings = @{}
$script:CheckFailures = 0
$script:OperationLock = $null
$script:OriginalTemp = @{}
$script:InstallStage = ''

function Assert-Release {
    $manifest=Join-Path $script:Root 'backend\release\SHA256SUMS.txt'
    if (-not (Test-Path -LiteralPath $manifest -PathType Leaf)) { throw 'Falta backend/release/SHA256SUMS.txt. Extraiga la entrega completa; no se modifica la instalacion.' }
    $entries=@{}
    foreach ($line in [IO.File]::ReadAllLines($manifest)) {
        if ($line -notmatch '^([0-9a-f]{64})  ([^\r\n]+)$') { throw 'Manifiesto de entrega no valido.' }
        $hash=$Matches[1]; $relative=$Matches[2]
        if ($relative -match '(^/|\\|:|(^|/)\.{1,2}(/|$)|//)' -or $entries.ContainsKey($relative)) { throw 'Ruta duplicada o no valida en el manifiesto.' }
        $entries[$relative]=$hash
        # Original documents and operator policy are mutable; executable code is not.
        $code=$relative -match '^(backend/(app|scripts|seeds|migrations)/|frontend/(dist|src)/)' -or $relative -match '^(instalar|iniciar|detener|diagnosticar)\.bat$' -or $relative -in @('backend/pyproject.toml','backend/uv.lock','backend/config/runtime-manifest.json','backend/config/apache/matrix-rh.conf.template','.htaccess')
        if (-not $code) { continue }
        $path=Join-Path $script:Root $relative
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw ('Entrega incompleta: ' + $relative) }
        if (((Get-Item -LiteralPath $path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash -ne $hash) { throw ('Integridad incorrecta: ' + $relative + '. Extraiga el ZIP completo en una carpeta nueva.') }
    }
    foreach ($required in @('backend/app/__init__.py','backend/app/main.py','backend/pyproject.toml','backend/uv.lock','backend/scripts/windows/MatrixRH.ps1','backend/scripts/windows/launch_process.py','backend/config/runtime-manifest.json','backend/config/apache/matrix-rh.conf.template','.htaccess','instalar.bat','iniciar.bat','detener.bat','diagnosticar.bat')) {
        if (-not $entries.ContainsKey($required)) { throw ('Falta entrada obligatoria de integridad: ' + $required) }
    }
    foreach ($directory in @('backend/app','backend/scripts','backend/seeds','backend/migrations')) {
        foreach ($file in Get-ChildItem -LiteralPath (Join-Path $script:Root $directory) -Recurse -File | Where-Object { $_.Extension -in @('.py','.ps1','.sql') }) {
            $relative=$file.FullName.Substring($script:Root.Length+1).Replace('\','/')
            if (-not $entries.ContainsKey($relative)) { throw ('Codigo fuera del manifiesto: ' + $relative) }
        }
    }
    $module=[IO.File]::ReadAllText((Join-Path $script:Root 'backend/app/__init__.py'))
    $project=[IO.File]::ReadAllText((Join-Path $script:Root 'backend/pyproject.toml'))
    $moduleVersion=[regex]::Match($module,'(?m)^__version__\s*=\s*["'']([^"'']+)["'']\s*$')
    $projectVersion=[regex]::Match($project,'(?m)^version\s*=\s*["'']([^"'']+)["'']\s*$')
    if (-not $moduleVersion.Success -or -not $projectVersion.Success -or $moduleVersion.Groups[1].Value -ne $projectVersion.Groups[1].Value) { throw 'La declaracion de version no coincide con pyproject.toml. No se modifica la instalacion.' }
}
function Say([string]$Message) { Write-Host ('[ OK ] ' + $Message) -ForegroundColor Green }
function Step([string]$Message) { Write-Host ('[ ...] ' + $Message) -ForegroundColor Cyan }
function Warn([string]$Message) { Write-Host ('[AVISO] ' + $Message) -ForegroundColor Yellow }
function Assert-PlainPath([string]$Path) {
    # Resolve each existing ancestor before writing: a junction can redirect a
    # syntactically local datadir, secret or runtime outside this installation.
    $candidate=[IO.Path]::GetFullPath($Path)
    while ($candidate) {
        if (Test-Path -LiteralPath $candidate) {
            $item=Get-Item -LiteralPath $candidate -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Una ruta operativa contiene un enlace o junction. Use carpetas fisicas propias; no se modifica el destino.' }
        }
        $parent=[IO.Path]::GetDirectoryName($candidate)
        if ($parent -eq $candidate) { break }
        $candidate=$parent
    }
}
function Assert-OperationalPaths {
    foreach ($relative in @('backend/config/.env','backend/config/secrets','backend/runtime','backend/logs','knowledge-base/documents','knowledge-base/state/mysql','knowledge-base/state/qdrant','knowledge-base/state/uploads','knowledge-base/state/run')) { Assert-PlainPath (Join-Path $script:Root $relative) }
}
function Ensure-Directory([string]$Path) { Assert-PlainPath $Path; [IO.Directory]::CreateDirectory($Path) | Out-Null }
function Write-Text([string]$Path, [string]$Text) {
    Assert-PlainPath $Path
    Ensure-Directory ([IO.Path]::GetDirectoryName($Path))
    [IO.File]::WriteAllText($Path, $Text, $script:Utf8)
}
function Invoke-Checked([string]$Executable, [string[]]$Arguments) {
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw ('Fallo ' + [IO.Path]::GetFileName($Executable) + ' (codigo ' + $LASTEXITCODE + '). Consulte el mensaje anterior.') }
}
function Quote-Argument([string]$Value) {
    # Windows CRT quoting, including trailing backslashes and literal quotes.
    if ($Value -notmatch '[\s"]' -and $Value.Length -gt 0) { return $Value }
    return '"' + ([regex]::Replace(([regex]::Replace($Value, '(\\*)"', '$1$1\"')), '(\\+)$', '$1$1')) + '"'
}
function Read-Settings {
    $script:Settings = @{}
    if (-not (Test-Path -LiteralPath $script:EnvPath -PathType Leaf)) { throw 'Falta backend/config/.env. Ejecute instalar.bat.' }
    foreach ($line in [IO.File]::ReadAllLines($script:EnvPath)) {
        if ($line -match '^\s*([A-Z][A-Z0-9_]*)\s*=\s*(.*?)\s*$') {
            $value = $Matches[2]
            if ($value.Length -ge 2 -and (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'")))) { $value = $value.Substring(1,$value.Length-2) }
            $script:Settings[$Matches[1]] = $value
        }
    }
}
function Setting([string]$Name, [string]$Default='') {
    if ($script:Settings.ContainsKey($Name)) { return [string]$script:Settings[$Name] }
    return $Default
}
function Set-Setting([string]$Name, [string]$Value) {
    if ($Value -match '[\r\n]') { throw 'Valor de configuracion no valido.' }
    $lines = [Collections.Generic.List[string]]::new()
    $found = $false
    foreach ($line in [IO.File]::ReadAllLines($script:EnvPath)) {
        if ($line -match ('^\s*' + [regex]::Escape($Name) + '\s*=')) { if (-not $found) { $lines.Add($Name + '=' + $Value); $found=$true } }
        else { $lines.Add($line) }
    }
    if (-not $found) { $lines.Add($Name + '=' + $Value) }
    Write-Text $script:EnvPath ([string]::Join([Environment]::NewLine,$lines) + [Environment]::NewLine)
    $script:Settings[$Name] = $Value
}
function Secure-Directory([string]$Path) {
    Ensure-Directory $Path
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    & icacls.exe $Path /inheritance:r /grant:r ('*' + $sid + ':(OI)(CI)F') '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'No se pudieron restringir permisos de los secretos.' }
}
function Secure-File([string]$Path) {
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    & icacls.exe $Path /inheritance:r /grant:r ('*' + $sid + ':F') '*S-1-5-18:F' '*S-1-5-32-544:F' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'No se pudieron restringir los permisos de configuracion privada.' }
}
function New-Secret([string]$Name, [bool]$ExistingDatabase) {
    $path = Join-Path $script:Secrets $Name
    Assert-PlainPath $path
    if (Test-Path -LiteralPath $path -PathType Leaf) {
        $secret = [IO.File]::ReadAllText($path).Trim()
        if ($secret -notmatch '^[0-9a-f]{64}$') { throw ('El secreto ' + $Name + ' no tiene el formato esperado; no se reemplaza.') }
        return $secret
    }
    if ($ExistingDatabase) { throw ('Hay datos MySQL pero falta ' + $Name + '. Restaure su respaldo completo; no se restablecen credenciales.') }
    $bytes = New-Object byte[] 32
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    $secret = ([BitConverter]::ToString($bytes)).Replace('-','').ToLowerInvariant()
    Write-Text $path $secret
    return $secret
}
function Assert-LocalConfiguration {
    if ((Setting 'APP_ENV') -ne 'standalone' -or (Setting 'AUTH_PROVIDER') -ne 'local') { throw 'Este controlador requiere APP_ENV=standalone y AUTH_PROVIDER=local.' }
    if ((Setting 'APP_HOST') -ne '127.0.0.1') { throw 'El backend nativo debe escuchar exclusivamente en 127.0.0.1.' }
    $web = [uri](Setting 'APP_BASE_URL')
    $ollama = [uri](Local-OllamaUrl)
    if (-not $web.IsLoopback -or $web.Scheme -ne 'http' -or -not $ollama.IsLoopback -or $ollama.Scheme -ne 'http') { throw 'Las direcciones de aplicacion y Ollama deben ser HTTP loopback para esta instalacion.' }
    $mysqlPort = [int](Setting 'MATRIX_MYSQL_PORT' '3308')
    $ports = @([int](Setting 'APP_PORT' '8000'),$web.Port,$ollama.Port,$mysqlPort)
    if (@($ports | Sort-Object -Unique).Count -ne 4 -or @($ports | Where-Object { $_ -lt 1024 -or $_ -gt 65535 }).Count -gt 0) { throw 'Configure cuatro puertos distintos entre 1024 y 65535.' }
    $expectedPaths=@{RAG_KNOWLEDGE_ROOT='knowledge-base\documents';QDRANT_PATH='knowledge-base\state\qdrant';UPLOAD_STORAGE_ROOT='knowledge-base\state\uploads';DATABASE_DATADIR_PATH='knowledge-base\state\mysql'}
    foreach ($name in $expectedPaths.Keys) {
        $value=Setting $name
        if (-not $value) { throw ('Falta la ruta propia ' + $name + ' en backend/config/.env.') }
        $actual=[IO.Path]::GetFullPath((Join-Path $script:Root $value))
        $expected=[IO.Path]::GetFullPath((Join-Path $script:Root $expectedPaths[$name]))
        if ($actual -ne $expected) { throw ($name + ' debe apuntar a su ruta propia dentro del proyecto.') }
        Assert-PlainPath $actual
    }
    if ((Setting 'QDRANT_MODE') -ne 'embedded') { throw 'Este controlador nativo requiere QDRANT_MODE=embedded.' }
    # secrets-scan: allow (regex de formato, sin credencial)
    if ((Setting 'DATABASE_URL') -notmatch ('^mysql\+pymysql://matrixrh:[0-9a-f]{64}@127\.0\.0\.1:' + $mysqlPort + '/matrix_rh\?charset=utf8mb4$')) { throw 'DATABASE_URL no corresponde a la base propia de esta instalacion; no se accede a otra base.' }
    foreach ($name in $script:Settings.Keys) {
        $inherited = [Environment]::GetEnvironmentVariable($name,'Process')
        if ($null -ne $inherited -and $inherited -ne (Setting $name)) { throw ('La variable heredada ' + $name + ' contradice backend/config/.env. No comparta su valor; use una consola sin ese override.') }
    }
}
function Get-Wamp {
    if ($WampRoot) { $candidate = [IO.Path]::GetFullPath($WampRoot) }
    else {
        $dir = Get-Item -LiteralPath $script:Root
        $candidate = ''
        while ($null -ne $dir) {
            if (Test-Path -LiteralPath (Join-Path $dir.FullName 'wampmanager.exe')) { $candidate=$dir.FullName; break }
            $dir=$dir.Parent
        }
    }
    if (-not $candidate -or -not (Test-Path -LiteralPath (Join-Path $candidate 'wampmanager.exe'))) { throw 'No se encontro WampServer sobre la carpeta del proyecto. Instale el proyecto dentro de su www.' }
    $www = [IO.Path]::GetFullPath((Join-Path $candidate 'www')) + [IO.Path]::DirectorySeparatorChar
    if (-not $script:Root.StartsWith($www,[StringComparison]::OrdinalIgnoreCase)) { throw 'El proyecto debe estar dentro del www del WampServer elegido.' }
    return $candidate
}
function Download-Pinned([string]$Url, [string]$Sha256, [string]$Destination) {
    if ($Sha256 -notmatch '^[0-9a-f]{64}$') { throw 'El paquete no tiene un SHA256 fijado valido.' }
    if ((Test-Path -LiteralPath $Destination) -and (Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash -eq $Sha256) { return }
    $allowed = @('github.com','release-assets.githubusercontent.com','objects.githubusercontent.com','releases.astral.sh','cdn.mysql.com','downloads.mysql.com')
    $current = [uri]$Url
    $temporary = $Destination + '.partial'
    Ensure-Directory ([IO.Path]::GetDirectoryName($Destination))
    try {
        for ($redirect=0; $redirect -le 6; $redirect++) {
            if ($current.Scheme -ne 'https' -or $allowed -notcontains $current.Host) { throw 'La descarga redirigio a un proveedor no autorizado.' }
            $request = [Net.HttpWebRequest]::Create($current)
            $request.AllowAutoRedirect=$false; $request.Timeout=30000; $request.ReadWriteTimeout=60000
            $request.UserAgent='Matrix-RH-Installer/1.4'
            $response=$request.GetResponse()
            if ([int]$response.StatusCode -ge 300 -and [int]$response.StatusCode -lt 400) {
                $location=$response.Headers['Location']; $response.Close(); $current=[uri]::new($current,$location); continue
            }
            try {
                $downloadStream=$response.GetResponseStream(); $output=[IO.File]::Create($temporary)
                try { $downloadStream.CopyTo($output) } finally { $output.Dispose(); $downloadStream.Dispose() }
            } finally { $response.Close() }
            if ((Get-FileHash -LiteralPath $temporary -Algorithm SHA256).Hash -ne $Sha256) { throw 'La descarga no coincide con el SHA256 fijado para esta entrega. No se extrae ni ejecuta.' }
            Move-Item -LiteralPath $temporary -Destination $Destination -Force
            return
        }
        throw 'Demasiadas redirecciones al descargar.'
    } finally { if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force } }
}
function Expand-Pinned([string]$Archive, [string]$Destination) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip=[IO.Compression.ZipFile]::OpenRead($Archive)
    $prefix=[IO.Path]::GetFullPath($Destination) + [IO.Path]::DirectorySeparatorChar
    try {
        foreach ($entry in $zip.Entries) {
            $path=[IO.Path]::GetFullPath((Join-Path $Destination $entry.FullName))
            if (-not $path.StartsWith($prefix,[StringComparison]::OrdinalIgnoreCase) -or $entry.FullName.Contains(':')) { throw 'Ruta no valida en el paquete ZIP.' }
        }
    } finally { $zip.Dispose() }
    [IO.Compression.ZipFile]::ExtractToDirectory($Archive,$Destination)
}
function Install-ZipRuntime([string]$Name, $Pin, [string]$Executable) {
    $target=Join-Path $script:Runtime $Name
    $receipt=Join-Path $target '.matrix-package.json'
    if (Test-Path -LiteralPath $Executable) {
        if (-not (Test-Path -LiteralPath $receipt)) { throw ('Runtime ' + $Name + ' sin registro de procedencia. No se reemplaza automaticamente.') }
        $stored=[IO.File]::ReadAllText($receipt) | ConvertFrom-Json
        if ($stored.sha256 -ne $Pin.sha256 -or (Get-FileHash -LiteralPath $Executable -Algorithm SHA256).Hash -ne $stored.executable_sha256) { throw ('Integridad o version inesperada de ' + $Name + '; conserve la carpeta para revisar.') }
        Say ($Name + ' ya instalado y verificado'); return
    }
    if (Test-Path -LiteralPath $target) { throw ('Directorio de runtime incompleto: ' + $Name + '. Conservelo y revise el fallo anterior.') }
    $archive=Join-Path $script:Runtime ('downloads\' + $Name + '-' + $Pin.version + '.zip')
    Step ('Descargando ' + $Name + ' ' + $Pin.version + ' desde su proveedor oficial; espere a que termine...')
    Download-Pinned $Pin.url $Pin.sha256 $archive
    $staging=$target + '.staging-' + [guid]::NewGuid().ToString('N')
    Expand-Pinned $archive $staging
    try {
        $relative=$Executable.Substring($target.Length+1)
        if (-not (Test-Path -LiteralPath (Join-Path $staging $relative))) { throw ('El ZIP ' + $Name + ' no contiene el ejecutable esperado.') }
        $record=@{version=$Pin.version;url=$Pin.url;sha256=$Pin.sha256;executable_sha256=(Get-FileHash -LiteralPath (Join-Path $staging $relative) -Algorithm SHA256).Hash}
        Write-Text (Join-Path $staging '.matrix-package.json') ($record | ConvertTo-Json)
        Move-Item -LiteralPath $staging -Destination $target
    } catch { throw ('No se pudo publicar ' + $Name + '. Se conserva la extraccion temporal. ' + $_.Exception.Message) }
}
function MySql-Version([string]$Executable) {
    if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) { throw 'El paquete MySQL esta incompleto: falta un ejecutable. No se inicializan datos.' }
    $arguments=@('--no-defaults','--version')
    if ([IO.Path]::GetFileName($Executable) -eq 'mysqladmin.exe') { $arguments=@('--no-defaults','--no-login-paths','--version') }
    $previousPreference=$ErrorActionPreference
    $nativeExit=-1; $banner=''
    try {
        $ErrorActionPreference='Continue'
        $banner=(& $Executable @arguments 2>$null | Out-String).Trim()
        $nativeExit=$LASTEXITCODE
    } catch { $nativeExit=-1 }
    finally { $ErrorActionPreference=$previousPreference }
    $version=[regex]::Match($banner,'\b(?:Ver|Distrib)\s+(8\.(?:0|4)\.\d+)\b')
    if ($nativeExit -ne 0 -or -not $version.Success -or $banner -notmatch '(Win64|x86_64)') {
        throw 'MySQL x64 no puede ejecutarse. Compruebe Microsoft Visual C++ 2019/2015-2022 x64 y el antivirus. No se inicializan datos ni se usa la base de WAMP.'
    }
    return $version.Groups[1].Value
}
function Install-MySql {
    $target=Join-Path $script:Runtime 'mysql'
    $exe=Join-Path $target 'bin\mysqld.exe'
    $client=Join-Path $target 'bin\mysqladmin.exe'
    $receipt=Join-Path $target '.matrix-package.json'
    if (Test-Path -LiteralPath $exe -PathType Leaf) {
        if (-not (Test-Path -LiteralPath $receipt -PathType Leaf)) { throw 'MySQL propio no tiene registro de procedencia; no se reemplaza.' }
        $record=[IO.File]::ReadAllText($receipt) | ConvertFrom-Json
        if ((Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash -ne $record.executable_sha256) { throw 'Cambio no esperado del ejecutable MySQL propio.' }
        if (-not (Test-Path -LiteralPath $client -PathType Leaf)) { throw 'MySQL propio esta incompleto: falta mysqladmin.exe. Restaure el runtime correspondiente a sus datos.' }
        if ($null -ne $record.PSObject.Properties['mysqladmin_sha256'] -and (Get-FileHash -LiteralPath $client -Algorithm SHA256).Hash -ne $record.mysqladmin_sha256) { throw 'Cambio no esperado de mysqladmin.exe.' }
        $version=MySql-Version $exe
        if ((MySql-Version $client) -ne $version) { throw 'Los ejecutables MySQL pertenecen a versiones distintas; no se mezclan.' }
        Say ('MySQL propio ' + $version + ' conservado; no se descarga ni actualiza su runtime.'); return
    }
    if (Test-Path -LiteralPath $target) { throw 'El runtime MySQL anterior esta incompleto; no se sobrescribe.' }
    $data=Join-Path $script:Root 'knowledge-base\state\mysql'
    if ((Test-Path -LiteralPath (Join-Path $script:Config 'mysql-initialized.json')) -or
        ((Test-Path -LiteralPath $data) -and @(Get-ChildItem -LiteralPath $data -Force).Count -gt 0)) {
        throw 'Hay datos MySQL pero falta su runtime. Restaure el binario y recibo correspondientes al respaldo; no se instala otra version sobre datos existentes.'
    }
    $pin=([IO.File]::ReadAllText((Join-Path $script:Config 'runtime-manifest.json')) | ConvertFrom-Json).mysql
    if ($pin.version -notmatch '^8\.4\.\d+$' -or $pin.archive_root -ne ('mysql-' + $pin.version + '-winx64') -or $pin.archive_name -ne ($pin.archive_root + '.zip')) { throw 'Manifiesto MySQL no valido.' }
    $archive=Join-Path $script:Runtime ('downloads\' + $pin.archive_name)
    Step ('Preparando MySQL Community ' + $pin.version + ' x64 desde el ZIP oficial; no requiere MySQL en WAMP...')
    Download-Pinned $pin.url $pin.sha256 $archive
    $staging=$target + '.staging-' + [guid]::NewGuid().ToString('N')
    Expand-Pinned $archive $staging
    $package=Join-Path $staging $pin.archive_root
    if (@(Get-ChildItem -LiteralPath $staging -Force).Count -ne 1 -or -not (Test-Path -LiteralPath $package -PathType Container)) { throw 'El ZIP MySQL no tiene la estructura verificada. Se conserva la extraccion; no se inicializan datos.' }
    foreach ($name in @('mysqld.exe','mysqladmin.exe')) {
        $binary=Join-Path $package ('bin\' + $name)
        if (-not (Test-Path -LiteralPath $binary -PathType Leaf) -or (Get-FileHash -LiteralPath $binary -Algorithm SHA256).Hash -ne $pin.binary_sha256.$name) { throw 'Un ejecutable MySQL no coincide con la entrega verificada. No se ejecuta ni se publica.' }
        if ((MySql-Version $binary) -ne $pin.version) { throw 'La version del ejecutable MySQL no coincide con el ZIP fijado.' }
    }
    $record=@{version=$pin.version;source='official_mysql_zip';url=$pin.url;sha256=$pin.sha256;verification='archive and binary SHA256 pinned; vendor GPG signature verified when preparing this release';executable_sha256=$pin.binary_sha256.'mysqld.exe';mysqladmin_sha256=$pin.binary_sha256.'mysqladmin.exe'}
    Write-Text (Join-Path $package '.matrix-package.json') ($record | ConvertTo-Json)
    Move-Item -LiteralPath $package -Destination $target
    [IO.Directory]::Delete($staging)
    Say 'MySQL propio instalado dentro de Matrix; se conservan intactos los servicios y datos de WAMP.'
}
function Process-Record([string]$Name) { return Join-Path $script:Run ($Name + '.json') }
function Owned-Process([string]$Name) {
    $path=Process-Record $Name
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    $record=[IO.File]::ReadAllText($path) | ConvertFrom-Json
    $process=Get-CimInstance Win32_Process -Filter ('ProcessId=' + [int]$record.pid) -ErrorAction SilentlyContinue
    if ($null -eq $process) { return $null }
    if ([string]::IsNullOrWhiteSpace($process.ExecutablePath) -or $null -eq $process.CreationDate) { throw ('No se puede verificar la identidad de ' + $Name + '. Ejecute este .bat como Administrador, con la misma elevacion de instalar.bat; no se toca el proceso.') }
    if ($process.ExecutablePath -ne $record.executable -or $process.CreationDate.ToUniversalTime().Ticks.ToString() -ne $record.created_ticks -or -not $record.executable.StartsWith($script:Runtime + '\',[StringComparison]::OrdinalIgnoreCase)) { Warn ('Registro antiguo de ' + $Name + ': ese PID corresponde a otro proceso y no se toca.'); return $null }
    return $process
}
function Port-Free([int]$Port) {
    $listener=New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback,$Port)
    try { $listener.Start(); return $true } catch { return $false } finally { $listener.Stop() }
}
function Start-Owned([string]$Name,[string]$Executable,[string[]]$Arguments,[hashtable]$Environment=@{}) {
    $existing=Owned-Process $Name
    if ($null -ne $existing) { return $existing.ProcessId }
    $stamp=[DateTime]::UtcNow.ToString('yyyyMMdd-HHmmssfff')
    $request=Join-Path $script:Run ('launch-' + $Name + '-' + [guid]::NewGuid().ToString('N') + '.json')
    $payload=@{executable=$Executable;arguments=@($Arguments);environment=$Environment;stdout=(Join-Path $script:Logs ($Name + '-' + $stamp + '.out.log'));stderr=(Join-Path $script:Logs ($Name + '-' + $stamp + '.err.log'))}
    Write-Text $request ($payload | ConvertTo-Json -Depth 4)
    try {
        $processIdText=& $script:Python -I (Join-Path $PSScriptRoot 'launch_process.py') $request
        if ($LASTEXITCODE -ne 0 -or [string]$processIdText -notmatch '^\d+$') { throw ($Name + ' no pudo iniciar; consulte backend/logs.') }
        $startedProcessId=[int]$processIdText
    } finally { if (Test-Path -LiteralPath $request) { Remove-Item -LiteralPath $request -Force } }
    Start-Sleep -Milliseconds 350
    $cim=Get-CimInstance Win32_Process -Filter ('ProcessId=' + $startedProcessId) -ErrorAction SilentlyContinue
    if ($null -eq $cim) { throw ($Name + ' termino al iniciar. Consulte backend/logs.') }
    Write-Text (Process-Record $Name) (@{pid=$cim.ProcessId;executable=$cim.ExecutablePath;created_ticks=$cim.CreationDate.ToUniversalTime().Ticks.ToString()} | ConvertTo-Json)
    return $cim.ProcessId
}
function Wait-Http([string]$Url,[int]$Seconds) {
    $end=[DateTime]::UtcNow.AddSeconds($Seconds)
    while ([DateTime]::UtcNow -lt $end) {
        try { $response=Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 5; if ($response.StatusCode -eq 200) { return } } catch { }
        Start-Sleep -Milliseconds 500
    }
    throw ('No respondio a tiempo ' + $Url + '. Ejecute diagnosticar.bat.')
}
function Invoke-Python([string[]]$Arguments) {
    if (-not (Test-Path -LiteralPath $script:Python)) { throw 'No existe el Python propio. Ejecute instalar.bat.' }
    $previous=$env:PYTHONPATH
    try { $env:PYTHONPATH=Join-Path $script:Root 'backend'; Invoke-Checked $script:Python $Arguments }
    finally { $env:PYTHONPATH=$previous }
}
function MySql-Client([string]$Command) {
    $exe=Join-Path $script:Runtime 'mysql\bin\mysqladmin.exe'
    $file=Join-Path $script:Secrets 'mysql-client.ini'
    # Windows PowerShell 5.1 turns redirected native stderr into ErrorRecords.
    # Connection refused while starting is an expected probe result, not a
    # terminating NativeCommandError. Never print the credentials file contents.
    $previousPreference=$ErrorActionPreference
    try {
        $ErrorActionPreference='Continue'
        & $exe ('--defaults-file=' + $file) --no-login-paths $Command 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    } finally { $ErrorActionPreference=$previousPreference }
}
function Complete-MySqlBootstrap {
    if (-not (MySql-Client 'status')) { throw 'MySQL propio no responde al control autenticado. Se conserva el bootstrap pendiente para reintentar.' }
    $initializedReceipt=Join-Path $script:Config 'mysql-initialized.json'
    if (-not (Test-Path -LiteralPath $initializedReceipt)) {
        $receipt=@{schema=1;datadir='knowledge-base/state/mysql';verified_utc=[DateTime]::UtcNow.ToString('o')} | ConvertTo-Json
        Write-Text $initializedReceipt $receipt
    }
    foreach ($path in @((Join-Path $script:Secrets 'mysql-init.sql'),(Join-Path $script:Run 'mysql-needs-bootstrap'))) {
        if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force }
    }
}
function Start-MySql {
    param([switch]$AllowInitialize)
    $port=[int](Setting 'MATRIX_MYSQL_PORT' '3308')
    $data=Join-Path $script:Root 'knowledge-base\state\mysql'
    Assert-PlainPath $data
    $initializedReceipt=Join-Path $script:Config 'mysql-initialized.json'
    $missingData=-not (Test-Path -LiteralPath (Join-Path $data 'mysql') -PathType Container)
    if ($missingData -and ((Test-Path -LiteralPath $initializedReceipt) -or -not $AllowInitialize)) { throw 'Falta el datadir MySQL de esta instalacion. No se crean ni reinicializan datos; restaure un respaldo coherente. Solo una instalacion nueva puede inicializar MySQL.' }
    $base=Join-Path $script:Runtime 'mysql'
    $exe=Join-Path $base 'bin\mysqld.exe'
    $ini=Join-Path $script:Config 'mysql.ini'
    $init=Join-Path $script:Secrets 'mysql-init.sql'
    if ($null -ne (Owned-Process 'mysql')) {
        Complete-MySqlBootstrap
        return
    }
    if (-not (Port-Free $port)) { throw ('Puerto MySQL ' + $port + ' ocupado por otro proceso. No se detiene.') }
    $serverLines=@('[mysqld]',('basedir="' + $base.Replace('\','/') + '"'),('datadir="' + $data.Replace('\','/') + '"'),('port=' + $port),'bind-address=127.0.0.1','mysqlx=0','skip-name-resolve','character-set-server=utf8mb4','collation-server=utf8mb4_unicode_ci',('log-error="' + (Join-Path $script:Logs 'mysql-error.log').Replace('\','/') + '"'),('pid-file="' + (Join-Path $script:Run 'mysqld.pid').Replace('\','/') + '"'))
    Write-Text $ini ([string]::Join([Environment]::NewLine,$serverLines))
    if (-not (Test-Path -LiteralPath (Join-Path $data 'mysql'))) {
        if ((Test-Path -LiteralPath $data) -and @(Get-ChildItem -LiteralPath $data -Force).Count -gt 0) { throw 'El datadir MySQL no esta vacio pero tampoco completo. No se reinicializa.' }
        Ensure-Directory $data
        Step 'Inicializando MySQL propio sin red...'
        Write-Text (Join-Path $script:Run 'mysql-needs-bootstrap') 'pending'
        Invoke-Checked $exe @(('--defaults-file=' + $ini),'--initialize-insecure','--skip-networking')
    }
    if (Test-Path -LiteralPath (Join-Path $script:Run 'mysql-needs-bootstrap')) {
        $rootPassword=[IO.File]::ReadAllText((Join-Path $script:Secrets 'mysql-root-password.txt')).Trim()
        $appPassword=[IO.File]::ReadAllText((Join-Path $script:Secrets 'mysql-app-password.txt')).Trim()
        $sql=@("ALTER USER 'root'@'localhost' IDENTIFIED BY '$rootPassword';","CREATE USER IF NOT EXISTS 'matrixrh'@'127.0.0.1' IDENTIFIED BY '$appPassword';","GRANT SELECT,INSERT,UPDATE,DELETE,CREATE,ALTER,INDEX,REFERENCES ON matrix_rh.* TO 'matrixrh'@'127.0.0.1';","CREATE USER IF NOT EXISTS 'matrix_owner'@'127.0.0.1' IDENTIFIED BY '$rootPassword';","GRANT ALL PRIVILEGES ON matrix_rh.* TO 'matrix_owner'@'127.0.0.1';","GRANT SHUTDOWN ON *.* TO 'matrix_owner'@'127.0.0.1';")
        Write-Text $init ([string]::Join([Environment]::NewLine,$sql))
    }
    $arguments=@('--defaults-file=' + $ini)
    if (Test-Path -LiteralPath $init) { $arguments+=('--init-file=' + $init) }
    Start-Owned 'mysql' $exe $arguments | Out-Null
    $end=[DateTime]::UtcNow.AddSeconds(90)
    while ([DateTime]::UtcNow -lt $end) { if (MySql-Client 'status') { break }; Start-Sleep -Milliseconds 500 }
    if (-not (MySql-Client 'status')) { throw 'MySQL propio no inicio en 90 segundos. Consulte backend/logs/mysql-error.log.' }
    Complete-MySqlBootstrap
    Say ('MySQL propio listo en 127.0.0.1:' + $port)
}
function Local-OllamaUrl {
    $url=$null
    if (-not [uri]::TryCreate((Setting 'OLLAMA_BASE_URL'),[UriKind]::Absolute,[ref]$url) -or
        $url.Scheme -ne 'http' -or -not $url.IsLoopback -or $url.UserInfo -or
        $url.AbsolutePath -ne '/' -or $url.Query -or $url.Fragment) {
        throw 'OLLAMA_BASE_URL debe ser HTTP loopback sin credenciales ni ruta; por ejemplo http://127.0.0.1:11434.'
    }
    return $url.AbsoluteUri.TrimEnd('/')
}
function Ollama-Inventory {
    $url=Local-OllamaUrl
    try { $inventory=Invoke-RestMethod -Uri ($url + '/api/tags') -TimeoutSec 10 -MaximumRedirection 0 }
    catch { throw ('No responde tu Ollama local en ' + $url + '. Abre Ollama con tu usuario de Windows y repite. Matrix no instala ni inicia otra instancia.') }
    if ($null -eq $inventory -or $null -eq $inventory.PSObject.Properties['models'] -or $inventory.models -isnot [array]) {
        throw 'El puerto configurado no devuelve un inventario valido de Ollama. Revisa OLLAMA_BASE_URL; no se descarga ningun modelo.'
    }
    return $inventory
}
function Assert-InstalledModels {
    $inventory=Ollama-Inventory
    foreach ($name in @((Setting 'OLLAMA_FAST_MODEL'),(Setting 'OLLAMA_DEEP_MODEL'),(Setting 'OLLAMA_EMBEDDING_MODEL')) | Select-Object -Unique) {
        if ($name -match '(cloud|https?://)' -or $name -notmatch '^[a-zA-Z0-9_./:-]+$') { throw 'Modelo remoto/no valido: esta instalacion usa modelos locales.' }
        $modelsFound=@($inventory.models | Where-Object { $null -ne $_ -and $null -ne $_.PSObject.Properties['name'] -and $_.name -ceq $name })
        if ($modelsFound.Count -eq 0) { throw ('Falta el modelo ' + $name + ' en tu Ollama local. Comprueba ollama list y las etiquetas de backend/config/.env. No se descargan modelos automaticamente.') }
        if ($modelsFound.Count -ne 1 -or $null -eq $modelsFound[0].PSObject.Properties['digest'] -or $modelsFound[0].digest -notmatch '^(sha256:)?[0-9a-fA-F]{64}$') { throw ('Inventario o digest invalido para ' + $name + '. No se sustituyen modelos.') }
        foreach ($property in @('remote_host','remote_model')) {
            if ($null -ne $modelsFound[0].PSObject.Properties[$property] -and $modelsFound[0].$property) { throw ('El modelo ' + $name + ' es remoto. Matrix requiere pesos locales.') }
        }
        Say ('Modelo local disponible: ' + $name)
    }
}
function Ensure-Models {
    Assert-InstalledModels
    Invoke-Python @('-m','scripts.bootstrap','pin-models')
    Secure-File $script:EnvPath
    Read-Settings
    Invoke-Python @('-m','scripts.model_smoke_test','--run-inference','--profile','all')
}
function Backend-Process {
    $identity=Join-Path $script:Run 'matrixrh-backend.identity.json'
    if (-not (Test-Path -LiteralPath $identity)) { return $null }
    $record=[IO.File]::ReadAllText($identity) | ConvertFrom-Json
    $process=Get-CimInstance Win32_Process -Filter ('ProcessId=' + [int]$record.pid) -ErrorAction SilentlyContinue
    if ($null -eq $process) { return $null }
    if ([string]::IsNullOrWhiteSpace($process.ExecutablePath) -or $null -eq $process.CreationDate) { throw 'No se puede verificar la identidad del backend. Ejecute este .bat como Administrador, con la misma elevacion de instalar.bat; no se toca el proceso.' }
    if ([IO.Path]::GetFullPath($record.root) -ne $script:Root) { throw 'La identidad backend activa pertenece a otra carpeta.' }
    $epoch=[datetime]::SpecifyKind([datetime]'1970-01-01',[DateTimeKind]::Utc)
    $created=($process.CreationDate.ToUniversalTime()-$epoch).TotalSeconds
    if ([math]::Abs($created-[double]$record.created_at) -gt 0.01 -or -not $process.ExecutablePath.StartsWith($script:Runtime + '\',[StringComparison]::OrdinalIgnoreCase)) { Warn 'La identidad backend es antigua; ese PID no se adopta ni se detiene.'; return $null }
    return $record
}
function Stop-Backend {
    $record=Backend-Process
    if ($null -eq $record) {
        if (-not (Port-Free ([int](Setting 'APP_PORT' '8000')))) { throw 'El puerto backend esta ocupado sin identidad propia. No se detiene ningun proceso.' }
        return
    }
    Write-Text (Join-Path $script:Run 'matrixrh-backend.stop') (@{pid=[int]$record.pid;created_at=[double]$record.created_at} | ConvertTo-Json -Compress)
    $end=[DateTime]::UtcNow.AddSeconds(45)
    while ([DateTime]::UtcNow -lt $end) { if (-not (Get-Process -Id $record.pid -ErrorAction SilentlyContinue)) { Say 'Backend detenido de forma cooperativa.'; return }; Start-Sleep -Milliseconds 250 }
    throw 'El backend no termino la parada cooperativa; los servicios de datos siguen activos para evitar perdida de trabajo.'
}
function Find-Apache([string]$Wamp) {
    $services=@(Get-CimInstance Win32_Service | Where-Object { $_.PathName -match 'httpd\.exe' -and $_.PathName.IndexOf($Wamp,[StringComparison]::OrdinalIgnoreCase) -ge 0 })
    if ($services.Count -ne 1) { throw 'No se identifico un unico servicio Apache de WAMP. Compruebe que WAMP esta instalado y su servicio registrado.' }
    $service=$services[0]
    if ($service.PathName -match '^\s*"([^"]+httpd\.exe)"') { $exe=$Matches[1] }
    elseif ($service.PathName -match '^\s*(\S+httpd\.exe)') { $exe=$Matches[1] }
    else { throw 'No se pudo resolver el ejecutable Apache registrado.' }
    $base=Split-Path -Parent (Split-Path -Parent $exe)
    return @{executable=$exe;config=(Join-Path $base 'conf\httpd.conf');base=$base;service=$service.Name}
}
function Configure-Apache([string]$Wamp) {
    $apache=Find-Apache $Wamp
    $proxy=Join-Path $script:Config 'apache\matrix-rh.conf'
    $template=Join-Path $script:Config 'apache\matrix-rh.conf.template'
    $web=[uri](Setting 'APP_BASE_URL')
    $proxyExisted=Test-Path -LiteralPath $proxy
    $proxyOriginal=$null
    if ($proxyExisted) { $proxyOriginal=[IO.File]::ReadAllBytes($proxy) }
    $text=[IO.File]::ReadAllText($template).Replace('{{PROJECT_ROOT}}',$script:Root.Replace('\','/')).Replace('{{WEB_PORT}}',[string]$web.Port).Replace('{{BACKEND_PORT}}',(Setting 'APP_PORT'))
    $original=[IO.File]::ReadAllBytes($apache.config)
    $body=[IO.File]::ReadAllText($apache.config)
    $updated=$body
    foreach ($module in @('proxy','proxy_http')) {
        if ($updated -notmatch ('(?m)^\s*LoadModule\s+' + $module + '_module\s')) {
            if (-not (Test-Path -LiteralPath (Join-Path $apache.base ('modules\mod_' + $module + '.so')))) { throw ('Apache no contiene mod_' + $module + '.so.') }
            $updated += [Environment]::NewLine + 'LoadModule ' + $module + '_module modules/mod_' + $module + '.so'
        }
    }
    $include='IncludeOptional "' + $proxy.Replace('\','/') + '"'
    if (-not $updated.Contains($include)) { $updated += [Environment]::NewLine + '# Matrix RH: include propio; retirar esta linea revierte la integracion.' + [Environment]::NewLine + $include + [Environment]::NewLine }
    $backup=Join-Path $script:Run ('apache-httpd-' + [DateTime]::UtcNow.ToString('yyyyMMdd-HHmmssfff') + '.conf.bak')
    [IO.File]::WriteAllBytes($backup,$original)
    try {
        Write-Text $proxy $text
        Write-Text $apache.config $updated
        Invoke-Checked $apache.executable @('-t','-f',$apache.config)
        $service=Get-Service -Name $apache.service
        if ($service.Status -eq 'Running') { Restart-Service -Name $apache.service -ErrorAction Stop }
        else { Start-Service -Name $apache.service -ErrorAction Stop }
    } catch {
        [IO.File]::WriteAllBytes($apache.config,$original)
        if ($proxyExisted) { [IO.File]::WriteAllBytes($proxy,$proxyOriginal) }
        elseif (Test-Path -LiteralPath $proxy) { Remove-Item -LiteralPath $proxy -Force }
        throw ('No se pudo aplicar Apache; se restauro httpd.conf. Ejecute instalar.bat como administrador. ' + $_.Exception.Message)
    }
    Write-Text (Join-Path $script:Run 'apache-integration.json') (@{service=$apache.service;config=$apache.config;backup=$backup;include=$proxy;url=$web.AbsoluteUri} | ConvertTo-Json)
    Say ('Apache WAMP configurado en ' + $web.AbsoluteUri)
}
function Start-Stack {
    Assert-Release
    Assert-LocalConfiguration
    Assert-InstalledModels
    Start-MySql
    Invoke-Python @('-m','scripts.installation_state','--root',$script:Root,'--guard-install')
    $port=[int](Setting 'APP_PORT' '8000')
    if (-not (Port-Free $port)) {
        if ($null -eq (Backend-Process)) { throw 'El puerto backend esta ocupado por otro proceso; no se reutiliza.' }
        Wait-Http ('http://127.0.0.1:' + $port + '/ready') 15
    } else {
        Invoke-Python @('-m','scripts.preflight')
        $old=$env:PYTHONPATH
        try {
            $env:PYTHONPATH=Join-Path $script:Root 'backend'
            $launchArguments=@('-B','-m','scripts.bootstrap','serve','--skip-preflight','--host','127.0.0.1','--port',[string]$port)
            $stamp=[DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss')
            Start-Process -FilePath $script:Python -ArgumentList (@($launchArguments | ForEach-Object { Quote-Argument $_ }) -join ' ') -WorkingDirectory $script:Root -WindowStyle Hidden -RedirectStandardOutput (Join-Path $script:Logs ('backend-' + $stamp + '.out.log')) -RedirectStandardError (Join-Path $script:Logs ('backend-' + $stamp + '.err.log')) | Out-Null
        } finally { $env:PYTHONPATH=$old }
        Wait-Http ('http://127.0.0.1:' + $port + '/health') 90
        Wait-Http ('http://127.0.0.1:' + $port + '/ready') 180
    }
    $apache=Find-Apache (Get-Wamp)
    if ((Get-Service -Name $apache.service).Status -ne 'Running') {
        try { Start-Service -Name $apache.service -ErrorAction Stop }
        catch { throw 'Apache WAMP esta detenido. Inicie WampServer o ejecute iniciar.bat como administrador para arrancar su servicio.' }
    }
    $web=(Setting 'APP_BASE_URL').TrimEnd('/')
    Wait-Http ($web + '/health') 20
    Say ('Matrix RH disponible: ' + $web)
    if (-not $NoBrowser) { Start-Process $web }
}
function Set-InstallStage {
    param(
        [ValidateSet('prerrequisitos','configuracion','modelos_locales','runtime_uv','runtime_mysql','runtime_python','validacion_modelos','estado_datos','migracion_sql','usuario_local','ingesta_documentos','apache_wamp','arranque','completada')][string]$Stage,
        [ValidateSet('running','failed','completed')][string]$Status='running'
    )
    $script:InstallStage=$Stage
    $progress=@{schema=1;stage=$Stage;status=$Status;updated_utc=[DateTime]::UtcNow.ToString('o')}
    Write-Text (Join-Path $script:Run 'installation-progress.json') ($progress | ConvertTo-Json)
}
function Get-InstallProgress {
    $path=Join-Path $script:Run 'installation-progress.json'
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $null }
    if ((Get-Item -LiteralPath $path).Length -gt 16384) { throw 'Registro de instalacion demasiado grande.' }
    $progress=[IO.File]::ReadAllText($path) | ConvertFrom-Json
    if ($progress.schema -ne 1 -or $progress.stage -notmatch '^[a-z_]{1,40}$' -or $progress.status -notin @('running','failed','completed')) { throw 'Registro de instalacion no valido.' }
    return $progress
}
function Show-InstallStage {
    try {
        $progress=Get-InstallProgress
        if ($null -eq $progress) { return }
        $status=@{running='pendiente o interrumpida';failed='interrumpida por error';completed='completada'}[$progress.status]
        Write-Host ('  Ultima etapa registrada: ' + $progress.stage + ' (' + $status + ').')
    } catch { Warn 'No se pudo leer el estado de instalacion; conserve el archivo para revisar.' }
}
function Test-InstalledRuntimes {
    $missing=[Collections.Generic.List[string]]::new()
    if (-not (Test-Path -LiteralPath $script:Python -PathType Leaf)) { $missing.Add('Python propio') }
    foreach ($relative in @('mysql/bin/mysqld.exe','mysql/bin/mysqladmin.exe','mysql/.matrix-package.json')) {
        if (-not (Test-Path -LiteralPath (Join-Path $script:Runtime $relative) -PathType Leaf)) { $missing.Add('MySQL propio'); break }
    }
    $unfinished=$false
    try {
        $progress=Get-InstallProgress
        $unfinished=($null -ne $progress -and ($progress.status -ne 'completed' -or $progress.stage -ne 'completada'))
    } catch { $unfinished=$true }
    if ($missing.Count -eq 0 -and -not $unfinished) { return $true }
    $script:CheckFailures++
    $detail=if ($missing.Count -gt 0) { 'falta ' + ($missing -join ' y ') } else { 'la preparacion no termino, aunque existen los ejecutables' }
    Write-Host ('[FALLA] Instalacion incompleta: ' + $detail + '.') -ForegroundColor Red
    Show-InstallStage
    Write-Host '         Ejecute instalar.bat como Administrador para reintentar la preparacion.'
    Write-Host '[PENDIENTE] Salud de MySQL, API, acceso web e indices: requieren completar la instalacion.' -ForegroundColor Yellow
    return $false
}
function Install-Stack {
    $principal=New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'La integracion con Apache requiere permisos de servicio. Cierre esta ventana y use clic derecho > Ejecutar como administrador en instalar.bat.' }
    Assert-Release
    Set-InstallStage 'prerrequisitos'
    foreach ($name in @('PYTHONHOME','UV_PYTHON_DOWNLOADS_JSON_URL','UV_PYTHON_INSTALL_MIRROR','UV_ASTRAL_MIRROR_URL','UV_INSECURE_HOST','UV_INDEX_URL','UV_EXTRA_INDEX_URL','UV_DEFAULT_INDEX','UV_INDEX')) {
        if ([Environment]::GetEnvironmentVariable($name,'Process')) { throw ('La variable heredada ' + $name + ' altera la procedencia del runtime. Use una consola sin ese override; no comparta su valor.') }
    }
    $wamp=Get-Wamp
    $disk=[IO.DriveInfo]::new([IO.Path]::GetPathRoot($script:Root))
    $minimum=5GB
    if (-not (Test-Path -LiteralPath $script:Python)) { $minimum=10GB }
    if ($disk.AvailableFreeSpace -lt $minimum) { throw ('Se requieren al menos ' + ($minimum/1GB) + ' GiB libres antes de instalar. No se han inicializado datos.') }
    if (-not (Test-Path -LiteralPath (Join-Path $script:Root 'frontend\dist\index.html'))) { throw 'Falta la interfaz compilada. Extraiga la entrega completa; no se requiere Node en este equipo.' }
    Set-InstallStage 'configuracion'
    Secure-Directory $script:Secrets
    $existingDb=Test-Path -LiteralPath (Join-Path $script:Root 'knowledge-base\state\mysql\mysql')
    $rootSecret=New-Secret 'mysql-root-password.txt' $existingDb
    $appSecret=New-Secret 'mysql-app-password.txt' $existingDb
    New-Secret 'local-admin-password.txt' $existingDb | Out-Null
    $appKey=New-Secret 'app-secret-key.txt' $existingDb
    if (-not (Test-Path -LiteralPath $script:EnvPath)) {
        if ($existingDb) { throw 'Hay datos pero falta configuracion; restaure el respaldo coherente. No se inicia una base diferente.' }
        Copy-Item -LiteralPath (Join-Path $script:Config 'env.example') -Destination $script:EnvPath
        Read-Settings
        Set-Setting 'APP_SECRET_KEY' $appKey
        Set-Setting 'DATABASE_URL' ('mysql+pymysql://matrixrh:' + $appSecret + '@127.0.0.1:' + (Setting 'MATRIX_MYSQL_PORT' '3308') + '/matrix_rh?charset=utf8mb4')
    } else { Read-Settings }
    Secure-File $script:EnvPath
    Assert-LocalConfiguration
    Set-InstallStage 'modelos_locales'
    Assert-InstalledModels
    Stop-Backend
    foreach ($service in @(@{name='mysql';port=[int](Setting 'MATRIX_MYSQL_PORT' '3308')})) {
        if ($null -eq (Owned-Process $service.name) -and -not (Port-Free $service.port)) { throw ('Puerto propio ocupado: ' + $service.name + ' ' + $service.port + '. No se modifica ningun proceso ajeno.') }
    }
    $mysqlPort=Setting 'MATRIX_MYSQL_PORT' '3308'
    # secrets-scan: allow (lectura de secreto privado en runtime, sin valor fijo)
    Write-Text (Join-Path $script:Secrets 'mysql-client.ini') ([string]::Join([Environment]::NewLine,@('[client]','user=matrix_owner',('password=' + $rootSecret),'host=127.0.0.1',('port=' + $mysqlPort),'protocol=tcp','connect-timeout=5')))
    $pins=[IO.File]::ReadAllText((Join-Path $script:Config 'runtime-manifest.json')) | ConvertFrom-Json
    Set-InstallStage 'runtime_uv'
    Install-ZipRuntime 'uv' $pins.uv (Join-Path $script:Runtime 'uv\uv.exe')
    Set-InstallStage 'runtime_mysql'
    Install-MySql
    Set-InstallStage 'runtime_python'
    $uv=Join-Path $script:Runtime 'uv\uv.exe'
    $uvEnvironment=@{UV_PYTHON_INSTALL_DIR=(Join-Path $script:Runtime 'python');UV_PYTHON_BIN_DIR=(Join-Path $script:Runtime 'python-bin');UV_CACHE_DIR=(Join-Path $script:Runtime 'cache');UV_PYTHON_CACHE_DIR=(Join-Path $script:Runtime 'cache\python');UV_PROJECT_ENVIRONMENT=(Join-Path $script:Runtime 'venv');UV_PYTHON_INSTALL_BIN='0';UV_PYTHON_CPYTHON_BUILD=$pins.python.build}
    $previous=@{}
    try {
        foreach ($key in $uvEnvironment.Keys) { $previous[$key]=[Environment]::GetEnvironmentVariable($key,'Process'); [Environment]::SetEnvironmentVariable($key,$uvEnvironment[$key],'Process') }
        Step ('Instalando Python propio ' + $pins.python.version + ' y dependencias fijadas por uv.lock...')
        Invoke-Checked $uv @('python','install',$pins.python.version,'--no-config')
        Invoke-Checked $uv @('sync','--project',(Join-Path $script:Root 'backend'),'--frozen','--no-dev','--no-editable','--python',$pins.python.version,'--managed-python','--no-python-downloads','--no-config')
    } finally { foreach ($key in $previous.Keys) { [Environment]::SetEnvironmentVariable($key,$previous[$key],'Process') } }
    Invoke-Python @('-m','scripts.preflight','--integrity-only')
    Set-InstallStage 'validacion_modelos'
    Ensure-Models
    Set-InstallStage 'estado_datos'
    Start-MySql -AllowInitialize
    Invoke-Python @('-m','scripts.installation_state','--root',$script:Root,'--guard-install')
    Set-InstallStage 'migracion_sql'
    Invoke-Python @('-m','scripts.bootstrap','migrate')
    Set-InstallStage 'usuario_local'
    Invoke-Python @('-m','scripts.local_identity','bootstrap','--username','Matrix','--password-file',(Join-Path $script:Secrets 'local-admin-password.txt'))
    Set-InstallStage 'ingesta_documentos'
    Invoke-Python @('-m','scripts.bootstrap','ingest')
    Set-InstallStage 'apache_wamp'
    Configure-Apache $wamp
    Set-InstallStage 'arranque'
    Start-Stack
    Set-InstallStage 'completada' 'completed'
    Say 'INSTALACION TERMINADA. Usuario inicial: Matrix.'
    Write-Host 'Consulte localmente backend/config/secrets/local-admin-password.txt y cambie la contrasena al entrar.'
}
function Stop-Stack {
    Assert-LocalConfiguration
    Stop-Backend
    Say 'Ollama local permanece activo; Matrix no detiene el servicio ni descarga modelos de memoria.'
    if ($null -ne (Owned-Process 'mysql')) {
        if (-not (MySql-Client 'shutdown')) { throw 'MySQL no acepto la parada autenticada. No se fuerza ni borra el datadir.' }
        $end=[DateTime]::UtcNow.AddSeconds(45)
        while ([DateTime]::UtcNow -lt $end -and $null -ne (Owned-Process 'mysql')) { Start-Sleep -Milliseconds 300 }
        if ($null -ne (Owned-Process 'mysql')) { throw 'MySQL no termino de cerrar; se conserva en ejecucion.' }
        Say 'MySQL propio detenido sin borrar datos.'
    }
    Say 'Matrix RH detenido. Apache WAMP permanece disponible para sus otros sitios.'
}
function Check([string]$Name,[scriptblock]$Body) {
    try { & $Body; Say $Name } catch { $script:CheckFailures++; Write-Host ('[FALLA] ' + $Name + ': ' + $_.Exception.Message) -ForegroundColor Red }
}
function Diagnose {
    $configured=Test-Path -LiteralPath $script:EnvPath -PathType Leaf
    if ($configured) {
        Read-Settings
        Check 'Configuracion aislada' { Assert-LocalConfiguration }
    } else {
        $script:CheckFailures++
        Write-Host '[FALLA] Instalacion incompleta: falta backend/config/.env. Ejecute instalar.bat.' -ForegroundColor Red
        Write-Host '[PENDIENTE] Ollama y servicios de Matrix: falta la configuracion de esta instalacion.' -ForegroundColor Yellow
    }
    Check 'Integridad del proyecto' { Assert-Release }
    Check 'Espacio libre (minimo operativo 5 GiB)' {
        $disk=[IO.DriveInfo]::new([IO.Path]::GetPathRoot($script:Root)); Write-Host ('  Libre: ' + [math]::Round($disk.AvailableFreeSpace/1GB,1) + ' GiB')
        if ($disk.AvailableFreeSpace -lt 5GB) { throw 'Espacio insuficiente para operacion segura.' }
    }
    if ($configured) {
        Check 'Ollama local y modelos instalados' {
            Assert-InstalledModels
            $loaded=Invoke-RestMethod -Uri ((Local-OllamaUrl) + '/api/ps') -TimeoutSec 10 -MaximumRedirection 0
            $names=@($loaded.models | ForEach-Object { $_.name })
            if ($names.Count -eq 0) { Write-Host '  Modelos cargados: ninguno; se cargan al consultar, sin descargar pesos.' }
            else { Write-Host ('  Modelos cargados: ' + ($names -join ', ')) }
        }
    }
    Check 'Apache WAMP instalado' { Find-Apache (Get-Wamp) | Out-Null }
    if ($configured -and (Test-InstalledRuntimes)) {
        Show-InstallStage
        Check 'Python propio' { Invoke-Python @('-c','import sys; print(sys.version.split()[0]); assert sys.version_info[:2] == (3, 12)') }
        Check 'MySQL propio y puerto' { if ($null -eq (Owned-Process 'mysql') -or -not (MySql-Client 'status')) { throw 'Servicio detenido o no disponible.' } }
        Check 'Backend listo (SQL, cola, RAG e identidad)' { if ($null -eq (Backend-Process)) { throw 'No existe un backend propio activo.' }; Wait-Http ('http://127.0.0.1:' + (Setting 'APP_PORT') + '/ready') 5 }
        Check 'Apache WAMP y acceso a la aplicacion' { Wait-Http ((Setting 'APP_BASE_URL').TrimEnd('/') + '/health') 5 }
        Check 'Fuentes, indice y archivos originales' { Invoke-Python @('-m','scripts.installation_state','--root',$script:Root,'--guard-install'); Invoke-Python @('-m','scripts.diagnosticar_rag','--root',$script:Root,'--username','Matrix') }
    }
    Write-Host 'Contenedores: NO APLICA; instalacion nativa Windows/WAMP.'
    Write-Host 'Ultimos errores (sin mostrar configuracion ni credenciales):'
    $logs=@(Get-ChildItem -LiteralPath $script:Logs -Filter '*.log' -File -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 6)
    foreach ($log in $logs) {
        foreach ($line in @(Get-Content -LiteralPath $log.FullName -Encoding UTF8 -Tail 80 | Select-String -Pattern '(ERROR|CRITICAL|Traceback|\[ERROR\])' | Select-Object -Last 5)) {
            $safe=[regex]::Replace([string]$line,'(?i)(mysql\+pymysql://|password\s*[=:]\s*)\S+','$1[OMITIDO]')
            Write-Host ('  ' + $log.Name + ': ' + $safe)
        }
    }
    Write-Host ('Resultado: ' + $script:CheckFailures + ' FALLA(S).')
    if ($script:CheckFailures -gt 0) { throw 'Diagnostico con fallos. Compare los componentes anteriores.' }
}

try {
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT -or -not [Environment]::Is64BitOperatingSystem -or -not [Environment]::Is64BitProcess) { throw 'Se requiere Windows x64 y PowerShell x64.' }
    [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12
    Assert-OperationalPaths
    foreach ($dir in @($script:Runtime,$script:Run,$script:Logs)) { Ensure-Directory $dir }
    $privateTemp=Join-Path $script:Runtime 'temp'
    Ensure-Directory $privateTemp
    foreach ($name in @('TEMP','TMP')) { $script:OriginalTemp[$name]=[Environment]::GetEnvironmentVariable($name,'Process'); [Environment]::SetEnvironmentVariable($name,$privateTemp,'Process') }
    $lockPath=Join-Path $script:Run 'operator.lock'
    try { $script:OperationLock=[IO.File]::Open($lockPath,[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None) }
    catch { throw 'Ya hay otra operacion Matrix en curso. Espere a que termine.' }
    Set-Location $script:Root
    Write-Host ('MATRIX RH - ' + $Action.ToUpperInvariant())
    if ($Action -notin @('instalar','diagnosticar')) { Read-Settings }
    switch ($Action) {
        'instalar' { Install-Stack }
        'iniciar' { Start-Stack }
        'detener' { Stop-Stack }
        'diagnosticar' { Diagnose }
    }
    exit 0
} catch {
    $failureMessage=$_.Exception.Message
    if ($Action -eq 'instalar' -and $script:InstallStage) {
        try { Set-InstallStage $script:InstallStage 'failed' } catch { Warn 'No se pudo registrar la etapa interrumpida.' }
    }
    Write-Host ('[FALLA] ' + $failureMessage) -ForegroundColor Red
    Write-Host 'No borre knowledge-base ni backend/config. Ejecute diagnosticar.bat y conserve el resultado.'
    exit 1
} finally {
    if ($null -ne $script:OperationLock) { $script:OperationLock.Dispose() }
    foreach ($name in $script:OriginalTemp.Keys) { [Environment]::SetEnvironmentVariable($name,$script:OriginalTemp[$name],'Process') }
}
