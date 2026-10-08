# Creado por Aldo Garcia.
<#
.SYNOPSIS
    Funciones compartidas por todos los scripts de Windows de Matrix RH.

.DESCRIPTION
    Los archivos .bat de la raiz no contienen logica: llaman a estos scripts, y
    estos delegan el diagnostico y la operacion en el modulo Python
    `scripts.bootstrap` / `scripts.preflight`. Un unico lugar donde vive la
    logica evita que el comportamiento del instalador y el de las pruebas se
    separen con el tiempo.
#>

Set-StrictMode -Version Latest

# Raiz del proyecto: este script vive en <raiz>\windows\
$script:MatrixRoot = Split-Path -Parent $PSScriptRoot
$script:VenvPython = Join-Path $script:MatrixRoot ".venv\Scripts\python.exe"
$script:BackendDir = Join-Path $script:MatrixRoot "backend"
$script:LogDir = Join-Path $script:MatrixRoot "var\logs"
$script:PidFile = Join-Path $script:MatrixRoot "var\matrixrh-backend.pid"

function Get-MatrixRoot { return $script:MatrixRoot }
function Get-MatrixLogDir { return $script:LogDir }
function Get-MatrixPidFile { return $script:PidFile }
function Get-MatrixBackendDir { return $script:BackendDir }

function Enter-MatrixOperationLock {
    <# El mismo mutex protege instalar/iniciar/detener, también entre sesiones. #>
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes(([IO.Path]::GetFullPath($script:MatrixRoot)).ToLowerInvariant())
        $suffix = ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '')
    }
    finally { $sha.Dispose() }
    $lock = [System.Threading.Mutex]::new($false, ('Global\MatrixRH-' + $suffix))
    $acquired = $false
    try {
        try { $acquired = $lock.WaitOne(0) }
        catch [System.Threading.AbandonedMutexException] { $acquired = $true }
        if (-not $acquired) { throw 'Otra operacion de Matrix RH esta en curso en esta carpeta. Termine esa operacion y vuelva a ejecutar.' }
        return $lock
    }
    catch { $lock.Dispose(); throw }
}

function Exit-MatrixOperationLock {
    param([System.Threading.Mutex]$Lock)
    if ($null -ne $Lock) {
        try { $Lock.ReleaseMutex() }
        finally { $Lock.Dispose() }
    }
}

function Get-MatrixRuntimeIdentity {
    $path = Join-Path $script:MatrixRoot 'var\matrixrh-backend.identity.json'
    try {
        $item = Get-Item -LiteralPath $path -ErrorAction Stop
        if ($item.Length -gt 4096 -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { return $null }
        $record = Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
        if (-not [string]::Equals($record.root, $script:MatrixRoot, [StringComparison]::OrdinalIgnoreCase)) { return $null }
        if ($record.pid -le 0 -or $record.created_at -le 0 -or $record.nonce -notmatch '^[0-9a-f]{32}$') { return $null }
        return $record
    }
    catch { return $null }
}

function Test-MatrixRuntimeIdentity {
    param($Candidate, $Record)
    if ($null -eq $Candidate -or $null -eq $Record -or
        -not $Candidate.PSObject.Properties['ProcessId'] -or -not $Candidate.PSObject.Properties['CreationDate']) { return $false }
    if ($null -eq $Candidate.CreationDate -or $Candidate.ProcessId -ne $Record.pid) { return $false }
    $epoch = [DateTime]::SpecifyKind([DateTime]'1970-01-01', [DateTimeKind]::Utc)
    $created = ($Candidate.CreationDate.ToUniversalTime() - $epoch).TotalSeconds
    return ([Math]::Abs($created - [double]$Record.created_at) -lt 0.001)
}

function Test-MatrixOwnedProcess {
    param($Candidate)
    if (-not $Candidate -or -not $Candidate.ExecutablePath -or -not $Candidate.CommandLine) { return $false }
    $isBackend = ($Candidate.CommandLine -match '(?:^|\s)-m\s+scripts\.bootstrap\s+serve(?:\s|$)' -or
         $Candidate.CommandLine -match '(?:^|\s)-m\s+uvicorn\s+app\.main:app(?:\s|$)')
    if (-not $isBackend) { return $false }
    if ([string]::Equals($Candidate.ExecutablePath, $script:VenvPython, [System.StringComparison]::OrdinalIgnoreCase)) { return $true }
    # El launcher de un venv puede delegar a Python base. Se exige además el
    # registro emitido por bootstrap en ESTA raíz y la creación exacta del PID.
    return ($Candidate.ExecutablePath -match '[\\/]pythonw?\.exe$' -and
        (Test-MatrixRuntimeIdentity $Candidate (Get-MatrixRuntimeIdentity)))
}

function Test-MatrixOwnedWorker {
    param($Candidate)
    if (Test-MatrixOwnedProcess $Candidate) { return $true }
    if (-not $Candidate -or -not $Candidate.ExecutablePath -or -not $Candidate.CommandLine) { return $false }
    return ([string]::Equals($Candidate.ExecutablePath, $script:VenvPython, [System.StringComparison]::OrdinalIgnoreCase) -and
        $Candidate.CommandLine -match '(?:^|\s)-m\s+scripts\.bootstrap\s+(?:ingest|ingest-watch)(?:\s|$)')
}

function Get-MatrixListeningBackend {
    try {
        $endpoint = [uri](Get-MatrixBackendUrl)
        $addresses = @([Net.Dns]::GetHostAddresses($endpoint.DnsSafeHost) | ForEach-Object { $_.ToString() })
        $listeners = @(Get-NetTCPConnection -LocalPort $endpoint.Port -State Listen -ErrorAction Stop |
            Where-Object { $_.LocalAddress -in $addresses -or $_.LocalAddress -in @('0.0.0.0', '::') })
        foreach ($listener in $listeners) {
            $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)" -ErrorAction Stop
            if (Test-MatrixOwnedProcess $process) { return $process }
            # Compatibilidad con launcher venv anterior al registro de identidad.
            if ($process -and $process.CommandLine -match '(?:^|\s)-m\s+scripts\.bootstrap\s+serve(?:\s|$)') {
                $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($process.ParentProcessId)" -ErrorAction Stop
                if ((Test-MatrixOwnedProcess $parent) -and $process.CreationDate -ge $parent.CreationDate) { return $process }
            }
        }
    }
    catch { }
    return $null
}

function Test-MatrixSameProcess {
    param($Expected, $Actual)
    return ($null -ne $Expected -and $null -ne $Actual -and
        $null -ne $Expected.CreationDate -and $null -ne $Actual.CreationDate -and
        $Expected.ProcessId -eq $Actual.ProcessId -and
        $Expected.CreationDate.ToUniversalTime().Ticks -eq $Actual.CreationDate.ToUniversalTime().Ticks)
}

function Stop-MatrixOwnedBackend {
    <# No mata por nombre. Captura identidad y descendientes antes del cierre. #>
    param([int]$ProcessId, [switch]$Force)
    $candidate = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
    if (Test-MatrixOwnedWorker $candidate) {
        $family = [System.Collections.Generic.List[object]]::new()
        $family.Add($candidate)
        $snapshot = @(Get-CimInstance Win32_Process -ErrorAction Stop)
        for ($index = 0; $index -lt $family.Count; $index++) {
            $parent = $family[$index]
            foreach ($child in $snapshot) {
                if ($child.ParentProcessId -eq $parent.ProcessId -and
                    $null -ne $child.CreationDate -and $child.CreationDate -ge $parent.CreationDate -and
                    -not ($family.ProcessId -contains $child.ProcessId)) { $family.Add($child) }
            }
        }
        if (-not $Force -and (Test-MatrixOwnedProcess $candidate)) {
            # PID + creación: una solicitud vieja no detiene un PID reutilizado.
            $stopFile = Join-Path $script:MatrixRoot "var\matrixrh-backend.stop"
            $epoch = [DateTime]::SpecifyKind([DateTime]'1970-01-01', [DateTimeKind]::Utc)
            $target = $candidate
            $registration = Get-MatrixRuntimeIdentity
            foreach ($member in $family) {
                if (Test-MatrixRuntimeIdentity $member $registration) { $target = $member; break }
            }
            $created = ($target.CreationDate.ToUniversalTime() - $epoch).TotalSeconds
            $request = @{ pid=$target.ProcessId; created_at=$created } | ConvertTo-Json -Compress
            Set-Content -LiteralPath $stopFile -Value $request -Encoding ASCII
            $stopDeadline = (Get-Date).AddSeconds(30)
            while ((Get-Date) -lt $stopDeadline) {
                $current = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
                if (-not (Test-MatrixSameProcess $candidate $current)) { break }
                Start-Sleep -Milliseconds 250
            }
            Remove-Item -LiteralPath $stopFile -ErrorAction SilentlyContinue
        }
        for ($index = $family.Count - 1; $index -ge 0; $index--) {
            $expected = $family[$index]
            $current = Get-CimInstance Win32_Process -Filter "ProcessId=$($expected.ProcessId)" -ErrorAction SilentlyContinue
            if (Test-MatrixSameProcess $expected $current) {
                # Mantener el objeto Process conserva su identidad nativa al terminar.
                $native = Get-Process -Id $current.ProcessId -ErrorAction SilentlyContinue
                if ($native -and [Math]::Abs(($native.StartTime.ToUniversalTime() - $current.CreationDate.ToUniversalTime()).TotalMilliseconds) -lt 1) {
                    Stop-Process -InputObject $native -Force -ErrorAction Stop
                    if (-not $native.WaitForExit(5000)) { throw "No se pudo detener PID $($current.ProcessId)." }
                }
            }
        }
    }
    if ((Test-Path $script:PidFile) -and ((Get-Content $script:PidFile -Raw).Trim() -eq [string]$ProcessId)) {
        Remove-Item $script:PidFile -ErrorAction SilentlyContinue
    }
}

function Write-Section {
    param([string]$Text)
    Write-Host ""
    Write-Host ("=" * 72) -ForegroundColor DarkCyan
    Write-Host "  $Text" -ForegroundColor Cyan
    Write-Host ("=" * 72) -ForegroundColor DarkCyan
}

function Write-Step { param([string]$Text) Write-Host "  -> $Text" -ForegroundColor Gray }
function Write-Ok { param([string]$Text) Write-Host "  [ OK ] $Text" -ForegroundColor Green }
function Write-Warn { param([string]$Text) Write-Host "  [WARN] $Text" -ForegroundColor Yellow }
function Write-Fail { param([string]$Text) Write-Host "  [FAIL] $Text" -ForegroundColor Red }

function Test-MatrixVenv {
    return (Test-Path $script:VenvPython)
}

function Get-MatrixEnvValue {
    <# Las variables del proceso tienen prioridad, igual que Settings. #>
    param([Parameter(Mandatory = $true)][string]$Key, [string]$Default = "")
    $inherited = [Environment]::GetEnvironmentVariable($Key)
    if ($null -ne $inherited) { return $inherited }
    $envFile = Join-Path $script:MatrixRoot ".env"
    $value = $Default
    if (Test-Path $envFile) {
        $pattern = '^\s*(?:export\s+)?' + [regex]::Escape($Key) + '\s*=\s*(.*)$'
        foreach ($line in Get-Content -LiteralPath $envFile -Encoding UTF8) {
            if ($line -match $pattern) {
                $raw = $Matches[1].Trim()
                if ($raw -match '^"(.*)"\s*(?:#.*)?$' -or $raw -match "^'(.*)'\s*(?:#.*)?$") {
                    $value = $Matches[1]
                }
                else { $value = ($raw -replace '\s+#.*$', '').Trim() }
            }
        }
    }
    return $value
}

function Get-MatrixPython {
    <#
        Devuelve el interprete a usar. Prioriza el venv del proyecto; si no
        existe, busca Python 3.12 x64 mediante el lanzador `py`.
    #>
    param([switch]$PreferSystem)
    if (-not $PreferSystem -and (Test-MatrixVenv)) { return $script:VenvPython }

    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) {
        try { $candidate = & py -3.12 -c "import sys; print(sys.executable)" 2>$null }
        catch { $candidate = $null }
        if ($candidate -and $LASTEXITCODE -eq 0) { return $candidate.Trim() }
    }
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) { return $python.Source }
    if ($PreferSystem -and (Test-MatrixVenv)) { return $script:VenvPython }
    return $null
}

function Assert-Python312 {
    <# Verifica que el interprete sea exactamente 3.12 y de 64 bits. #>
    param([string]$PythonPath)

    if (-not $PythonPath) {
        Write-Fail "No se encontro ningun interprete de Python."
        Write-Host "         Instale Python 3.12 x64 desde https://www.python.org/downloads/"
        return $false
    }
    $info = & $PythonPath -c "import sys,platform; print(f'{sys.version_info.major}.{sys.version_info.minor}|{platform.architecture()[0]}')" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "No fue posible ejecutar el interprete: $PythonPath"
        return $false
    }
    $parts = $info.Trim().Split('|')
    if ($parts[0] -ne "3.12") {
        Write-Fail "Se requiere Python 3.12 (encontrado $($parts[0]))."
        return $false
    }
    if ($parts[1] -ne "64bit") {
        Write-Fail "Se requiere Python 3.12 de 64 bits (encontrado $($parts[1]))."
        return $false
    }
    Write-Ok "Python 3.12 x64: $PythonPath"
    return $true
}

function Find-MatrixUv {
    <# Reutiliza uv instalado, incluso cuando Scripts aun no esta en PATH. #>
    param([string]$PythonPath)
    $command = Get-Command uv -ErrorAction SilentlyContinue
    if ($command) { return $command }
    $localTool = Join-Path $script:MatrixRoot 'var\tools\uv\Scripts\uv.exe'
    if (Test-Path -LiteralPath $localTool -PathType Leaf) {
        return (Get-Command $localTool -ErrorAction SilentlyContinue)
    }
    if (-not $PythonPath) { return $null }
    try {
        $paths = @(& $PythonPath -c "import sysconfig; print(sysconfig.get_path('scripts')); print(sysconfig.get_path('scripts', 'nt_user'))" 2>$null)
        if ($LASTEXITCODE -ne 0) { return $null }
        foreach ($directory in $paths) {
            if (-not $directory) { continue }
            $candidate = Join-Path $directory.Trim() 'uv.exe'
            if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                return (Get-Command $candidate -ErrorAction SilentlyContinue)
            }
        }
    }
    catch { return $null }
    return $null
}

function Initialize-MatrixUv {
    <# Solo provisiona el gestor fijado. El backend siempre usa uv.lock. #>
    param([Parameter(Mandatory = $true)][string]$PythonPath, [switch]$Offline)
    $existing = Find-MatrixUv -PythonPath $PythonPath
    if ($existing) { return $existing }
    if ($Offline) {
        Write-Fail 'Falta uv y se solicito -Offline. Prepare uv >=0.11.33,<0.13.0 y su cache antes de instalar sin red.'
        return $null
    }
    $toolRoot = Join-Path $script:MatrixRoot 'var\tools\uv'
    $toolPython = Join-Path $toolRoot 'Scripts\python.exe'
    $toolExe = Join-Path $toolRoot 'Scripts\uv.exe'
    $requirements = Join-Path $script:MatrixRoot 'windows\uv-bootstrap-requirements.txt'
    if (-not (Test-Path -LiteralPath $requirements -PathType Leaf)) {
        Write-Fail 'Falta windows\uv-bootstrap-requirements.txt; extraiga la entrega completa.'
        return $null
    }
    Write-Step 'Instalando uv 0.12.19 en var\tools\uv (requiere acceso al indice de paquetes)...'
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        if (-not (Test-Path -LiteralPath $toolPython -PathType Leaf)) {
            & $PythonPath -m venv $toolRoot 2>&1 | ForEach-Object { Write-Host $_ }
            if ($LASTEXITCODE -ne 0) { Write-Fail 'No se pudo crear el entorno local de uv.'; return $null }
        }
        & $toolPython -m pip install --disable-pip-version-check --no-input --only-binary=:all: --no-deps --require-hashes -r $requirements 2>&1 | ForEach-Object { Write-Host $_ }
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $toolExe -PathType Leaf)) {
            Write-Fail 'No se pudo obtener uv. Verifique acceso a PyPI o al indice corporativo configurado y repita.'
            return $null
        }
        return (Get-Command $toolExe -ErrorAction SilentlyContinue)
    }
    finally { $ErrorActionPreference = $previousPreference }
}

function Invoke-MatrixPython {
    <#
        Ejecuta un modulo Python del backend con el PYTHONPATH correcto.
        Devuelve UNICAMENTE el codigo de salida del proceso.

        La salida del proceso se envia a la consola con Out-Host y no al flujo de
        salida de la funcion: en PowerShell, todo lo que una funcion emite forma
        parte de su valor de retorno, de modo que sin Out-Host el llamador
        recibiria un arreglo con las lineas del diagnostico mas el codigo, y una
        comparacion como `if ($code -ne 0)` seria siempre verdadera.
    #>
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [switch]$Quiet,
        [switch]$AllowSystemPython
    )
    # La operacion normal exige el venv. La sonda previa de puertos usa solo
    # stdlib y puede ejecutarse con el Python disponible antes de crear .venv.
    if (-not $AllowSystemPython -and -not (Test-MatrixVenv)) {
        if (-not $Quiet) {
            Write-Fail "No existe el entorno virtual del proyecto (.venv)."
            Write-Host "         Ejecute INSTALAR_MATRIX_RH.bat antes de este comando."
        }
        return 126
    }

    $python = if ($AllowSystemPython) { Get-MatrixPython -PreferSystem } else { Get-MatrixPython }
    if (-not $python) { return 127 }

    $previous = $env:PYTHONPATH
    $previousPreference = $ErrorActionPreference
    $env:PYTHONPATH = $script:BackendDir
    try {
        Push-Location $script:BackendDir
        # 'Continue' es imprescindible: PowerShell convierte CUALQUIER escritura a
        # stderr de un ejecutable nativo en un registro de error y la envuelve en
        # un NativeCommandError. Con eso, el mensaje accionable de Python queda
        # sepultado bajo "Traceback (most recent call last)" y el operador no ve
        # la causa real. Aqui stderr se trata como texto normal.
        $ErrorActionPreference = 'Continue'
        if ($Quiet) {
            & $python @Arguments 2>&1 | Out-Null
        }
        else {
            & $python @Arguments 2>&1 | ForEach-Object { Write-Host $_ }
        }
        return $LASTEXITCODE
    }
    finally {
        Pop-Location
        $env:PYTHONPATH = $previous
        $ErrorActionPreference = $previousPreference
    }
}

function Test-MatrixDependencies {
    <#
        Tener python.exe no garantiza que uv haya completado la instalacion.
        El checker usa solo la biblioteca estandar para detectar los paquetes
        ausentes y nunca carga .env ni conecta MySQL/Ollama/Qdrant.
        Invoke-MatrixPython envia los mensajes a consola y devuelve solo el codigo.
    #>
    $code = Invoke-MatrixPython -Arguments @("-m", "scripts.preflight", "--dependencies-only")
    return ($code -eq 0)
}

function Test-MatrixIntegrity {
    <# Solo stdlib: no importa app, no abre .env ni escribe estado runtime. #>
    $code = Invoke-MatrixPython -AllowSystemPython -Arguments @(
        "-S", "-m", "scripts.preflight", "--integrity-only", "--require-manifest"
    )
    return ($code -eq 0)
}

function Test-MatrixApplication {
    <# Detecta imports ASGI invalidos antes de modificar la base o ingerir. #>
    $code = Invoke-MatrixPython -Arguments @("-m", "scripts.preflight", "--app-only")
    return ($code -eq 0)
}

function Test-MatrixSafety {
    $code = Invoke-MatrixPython -Arguments @("-m", "scripts.preflight", "--safety-only", "--read-only")
    return ($code -eq 0)
}

function Test-MatrixDatabaseFree {
    <#
        Comprueba si una base MySQL esta libre para Matrix RH.

        Devuelve una tabla con Libre = $true si la base no existe o solo contiene
        la tabla de control de Matrix RH; $false si ya tiene tablas de otra
        aplicacion. Es de SOLO LECTURA: no crea ni modifica nada.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$DatabaseUrl,
        [Parameter(Mandatory = $true)][string]$DatabaseName
    )

    # El import va DENTRO del try: si se ejecuta con un interprete sin las
    # dependencias del proyecto, un ImportError a nivel de modulo escaparia como
    # traceback y el llamador lo confundiria con el veredicto.
    $codigo = @"
import json
import re
import sys

try:
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    from app.database.migrator import applied_versions, inspect_ownership
except ImportError:
    print('DESCONOCIDO:SinDependencias')
    sys.exit(0)

try:
    arguments = json.loads(sys.stdin.buffer.read().decode('utf-8-sig'))
    nombre = arguments['name']
    if not re.fullmatch(r'[A-Za-z0-9_]{1,64}', nombre):
        raise ValueError('NombreInvalido')
    url = make_url(arguments['url']).set(database='')
    engine = create_engine(url, pool_pre_ping=True, future=True, connect_args={'connect_timeout': 5})
    with engine.connect() as conn:
        existe = conn.execute(
            text('SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA WHERE SCHEMA_NAME = :n'),
            {'n': nombre},
        ).first()
        # LIBRE = la base NO existe, asi que Matrix RH puede crearla. Una base
        # que ya existe nunca es 'LIBRE', aunque este vacia: pudo crearla otra
        # aplicacion.
        if not existe:
            print('LIBRE')
            sys.exit(0)
    engine.dispose()
    engine = create_engine(url.set(database=nombre), pool_pre_ping=True, future=True,
                           connect_args={'connect_timeout': 5})
    try:
        propia, tablas = inspect_ownership(engine)
        if propia:
            print('PROPIA' if applied_versions(engine) else 'EXISTE_VACIA')
            sys.exit(0)
    finally:
        engine.dispose()
    # Un registro incompatible tambien pertenece a otra aplicacion, aunque sea
    # la unica tabla. Solo el migrador decide la adopcion explicita de una vacia.
    print('OCUPADA:' + ','.join(sorted(tablas)[:6]))
except Exception as exc:
    print('DESCONOCIDO:' + type(exc).__name__)
"@

    $temporal = Join-Path $env:TEMP "matrixrh-dbcheck-$([guid]::NewGuid().ToString('N')).py"
    Set-Content -Path $temporal -Value $codigo -Encoding UTF8
    $previousPreference = $ErrorActionPreference
    $previousPythonPath = $env:PYTHONPATH
    $previousEncoding = $OutputEncoding
    try {
        $python = Get-MatrixPython
        if (-not $python) { return @{ Libre = $false; Detalle = 'DESCONOCIDO:SinPython' } }
        $ErrorActionPreference = 'Continue'
        $env:PYTHONPATH = $script:BackendDir
        $OutputEncoding = New-Object System.Text.UTF8Encoding($false)
        # Las credenciales viajan por stdin, nunca en la linea de comandos.
        $payload = @{ name = $DatabaseName; url = $DatabaseUrl } | ConvertTo-Json -Compress
        $salida = ("$($payload | & $python $temporal 2>&1 | Select-Object -Last 1)").Trim()

        # Solo cuatro respuestas son un veredicto. Cualquier otra cosa (una linea
        # de traceback, una advertencia del interprete) significa que NO se pudo
        # determinar, y debe tratarse como tal en lugar de asumir lo peor.
        $veredictos = @('LIBRE', 'PROPIA', 'EXISTE_VACIA')
        if ($veredictos -notcontains $salida -and $salida -notlike 'OCUPADA:*') {
            return @{ Libre = $false; Detalle = "DESCONOCIDO:$salida" }
        }
        # LIBRE  = no existe, Matrix RH la creara.
        # PROPIA = instalacion previa de Matrix RH, se reutiliza.
        # EXISTE_VACIA / OCUPADA = existe y no es nuestra: hay que elegir otro
        #   nombre, aunque este vacia.
        return @{ Libre = ($salida -eq 'LIBRE' -or $salida -eq 'PROPIA'); Detalle = $salida }
    }
    finally {
        $ErrorActionPreference = $previousPreference
        $env:PYTHONPATH = $previousPythonPath
        $OutputEncoding = $previousEncoding
        Remove-Item $temporal -ErrorAction SilentlyContinue
    }
}

function Set-MatrixEnvValue {
    <# Reemplaza una clave del .env conservando el resto del archivo. #>
    param(
        [Parameter(Mandatory = $true)][string]$Key,
        [Parameter(Mandatory = $true)][string]$Value
    )
    $envFile = Join-Path $script:MatrixRoot ".env"
    if (-not (Test-Path $envFile)) { return $false }
    $contenido = Get-Content -LiteralPath $envFile -Encoding UTF8
    # No usar Value como reemplazo regex: una contrasena con $1/$& se alteraria.
    $found = $false
    $nuevo = @(foreach ($line in $contenido) {
        if ($line -match "^\s*(?:export\s+)?$([regex]::Escape($Key))\s*=") { "$Key=$Value"; $found = $true }
        else { $line }
    })
    if (-not $found) { $nuevo += "$Key=$Value" }
    [System.IO.File]::WriteAllLines($envFile, [string[]]$nuevo, (New-Object System.Text.UTF8Encoding($false)))
    return $true
}

function Initialize-MatrixSeedPassword {
    <# Una clave local aleatoria; nunca se reescribe ni se imprime en logs. #>
    $profile = (Get-MatrixEnvValue -Key "APP_ENV" -Default "development").ToLowerInvariant()
    $seed = (Get-MatrixEnvValue -Key "LOCAL_TEST_SEED_USERS_ENABLED" -Default "true").ToLowerInvariant()
    if (@("development", "test") -notcontains $profile -or @("1", "true", "yes", "on", "t", "y") -notcontains $seed) {
        return $true
    }
    if (Get-MatrixEnvValue -Key "MATRIX_SEED_PASSWORD") {
        Write-Ok "MATRIX_SEED_PASSWORD presente; se conserva y no se reinician usuarios existentes."
        return $true
    }
    if ($null -ne [Environment]::GetEnvironmentVariable("MATRIX_SEED_PASSWORD")) {
        Write-Fail "MATRIX_SEED_PASSWORD del proceso esta vacia. Retire esa variable heredada y repita."
        return $false
    }
    $bytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    $password = -join ($bytes | ForEach-Object { $_.ToString("x2") })
    if (-not (Set-MatrixEnvValue -Key "MATRIX_SEED_PASSWORD" -Value $password)) { return $false }
    Write-Ok "Se genero MATRIX_SEED_PASSWORD una sola vez en .env; consulte ese archivo localmente para el primer login."
    Write-Host "         Un usuario existente conserva su contrasena; el seed nunca la reinicia."
    return $true
}

function Test-MatrixEnvFile {
    <# Crea .env a partir de .env.example la primera vez. Nunca lo sobrescribe. #>
    $envFile = Join-Path $script:MatrixRoot ".env"
    $example = Join-Path $script:MatrixRoot ".env.example"
    if (Test-Path $envFile) {
        Write-Ok "Archivo .env presente (no se sobrescribe)."
        return $true
    }
    if (-not (Test-Path $example)) {
        Write-Fail "No existe .env.example; el paquete esta incompleto."
        return $false
    }
    Copy-Item $example $envFile
    # La clave de firma se genera localmente: nunca viaja en el paquete.
    $secretBytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($secretBytes) } finally { $rng.Dispose() }
    $secret = -join ($secretBytes | ForEach-Object { $_.ToString("x2") })
    $newContent = (Get-Content -LiteralPath $envFile -Encoding UTF8) -replace '^APP_SECRET_KEY=.*', "APP_SECRET_KEY=$secret"
    [System.IO.File]::WriteAllLines($envFile, [string[]]$newContent, (New-Object System.Text.UTF8Encoding($false)))
    Write-Ok "Se creo .env a partir de .env.example con una APP_SECRET_KEY nueva."
    Write-Warn "Revise DATABASE_URL en .env antes de continuar si su MySQL no es el de WAMP."
    return $true
}

function Get-MatrixBackendUrl {
    <# Lee host y puerto del .env; usa los valores por defecto si faltan. #>
    $hostName = Get-MatrixEnvValue -Key "APP_HOST" -Default "127.0.0.1"
    $port = Get-MatrixEnvValue -Key "APP_PORT" -Default "8000"
    if ($hostName -eq "0.0.0.0") { $hostName = "127.0.0.1" }
    if ($hostName -eq "::") { $hostName = "::1" }
    if ($hostName.Contains(":")) { $hostName = "[$($hostName.Trim('[', ']'))]" }
    return "http://${hostName}:${port}"
}

function Test-MatrixBackendPortFree {
    <# Misma comprobacion stdlib que el preflight, antes de instalar paquetes. #>
    $hostName = Get-MatrixEnvValue -Key "APP_HOST" -Default "127.0.0.1"
    $rawPort = Get-MatrixEnvValue -Key "APP_PORT" -Default "8000"
    $code = Invoke-MatrixPython -AllowSystemPython -Arguments @(
        "-m", "scripts.preflight", "--ports-only", "--host=$hostName", "--port=$rawPort"
    )
    return ($code -eq 0)
}

function Get-MatrixServiceEndpoints {
    <# Solo devuelve host/puerto; nunca imprime credenciales de DATABASE_URL. #>
    $backend = [uri](Get-MatrixBackendUrl)
    @{ Name = "Backend"; HostName = $backend.DnsSafeHost; Port = $backend.Port }
    $database = [uri](Get-MatrixEnvValue -Key "DATABASE_URL" -Default "mysql+pymysql://root:@127.0.0.1:3306/matrix_rh_131")
    @{ Name = "MySQL"; HostName = $database.DnsSafeHost; Port = $(if ($database.Port -gt 0) { $database.Port } else { 3306 }) }
    $providers = @(
        (Get-MatrixEnvValue -Key "LLM_PROVIDER" -Default "ollama"),
        (Get-MatrixEnvValue -Key "LLM_DEEP_PROVIDER" -Default "ollama"),
        (Get-MatrixEnvValue -Key "LLM_EMBEDDING_PROVIDER" -Default "ollama")
    ) | Select-Object -Unique
    foreach ($provider in $providers) {
        if ($provider -eq "ollama") {
            $endpoint = [uri](Get-MatrixEnvValue -Key "OLLAMA_BASE_URL" -Default "http://127.0.0.1:11434")
            @{ Name = "Ollama"; HostName = $endpoint.DnsSafeHost; Port = $endpoint.Port }
        }
        elseif ($provider -eq "openai_compatible") {
            $endpoint = [uri](Get-MatrixEnvValue -Key "LLM_API_BASE_URL" -Default "http://127.0.0.1:8080/v1")
            @{ Name = "Inferencia compatible"; HostName = $endpoint.DnsSafeHost; Port = $endpoint.Port }
        }
    }
    if ((Get-MatrixEnvValue -Key "QDRANT_MODE" -Default "embedded") -eq "server") {
        $endpoint = [uri](Get-MatrixEnvValue -Key "QDRANT_URL" -Default "http://127.0.0.1:6333")
        @{ Name = "Qdrant server"; HostName = $endpoint.DnsSafeHost; Port = $endpoint.Port }
    }
}

function Test-MatrixBackendUp {
    <# True si /health responde y la aplicacion se identifica como Matrix RH. #>
    param([int]$TimeoutSec = 3)
    try {
        $response = Invoke-RestMethod -Uri "$(Get-MatrixBackendUrl)/health" -TimeoutSec $TimeoutSec
        return ($response.app -eq "Matrix RH")
    }
    catch { return $false }
}

function Get-MatrixReadinessDetail {
    <# Lee el cuerpo JSON también en 503; no oculta un lease legado ocupado. #>
    param([int]$TimeoutSec = 5)
    $response = $null
    $reader = $null
    try {
        $request = [System.Net.HttpWebRequest]::Create("$(Get-MatrixBackendUrl)/ready")
        $request.Proxy = $null
        $request.AllowAutoRedirect = $false
        $request.Timeout = $TimeoutSec * 1000
        $request.ReadWriteTimeout = $TimeoutSec * 1000
        try { $response = $request.GetResponse() }
        catch [System.Net.WebException] {
            $response = $_.Exception.Response
            if ($null -eq $response) { throw }
        }
        $reader = [System.IO.StreamReader]::new($response.GetResponseStream())
        return ($reader.ReadToEnd() | ConvertFrom-Json)
    }
    finally {
        if ($null -ne $reader) { $reader.Dispose() }
        if ($null -ne $response) { $response.Close() }
    }
}

function Test-MatrixBackendReady {
    param([int]$TimeoutSec = 5)
    try { return [bool](Get-MatrixReadinessDetail -TimeoutSec $TimeoutSec).ready }
    catch { return $false }
}

function Test-OllamaModels {
    <# Nombre conservado por compatibilidad; prueba el adapter configurado. #>
    $code = Invoke-MatrixPython -Arguments @("-m", "scripts.preflight", "--llm-only")
    return ($code -eq 0)
}

function Test-MatrixOllamaUp {
    <# Sonda local acotada; no usa proxy ni sigue redirecciones. #>
    param([Parameter(Mandatory = $true)][uri]$Endpoint)
    $response = $null
    $reader = $null
    try {
        $request = [System.Net.HttpWebRequest]::Create($Endpoint.AbsoluteUri.TrimEnd('/') + '/api/version')
        $request.Proxy = $null
        $request.AllowAutoRedirect = $false
        $request.Timeout = 2000
        $request.ReadWriteTimeout = 2000
        $response = $request.GetResponse()
        $reader = New-Object System.IO.StreamReader($response.GetResponseStream())
        $payload = $reader.ReadToEnd() | ConvertFrom-Json
        return ($null -ne $payload -and $null -ne $payload.PSObject.Properties['version'] -and
            -not [string]::IsNullOrWhiteSpace([string]$payload.version))
    }
    catch { return $false }
    finally {
        if ($null -ne $reader) { $reader.Dispose() }
        if ($null -ne $response) { $response.Close() }
    }
}

function Find-MatrixOllama {
    $command = Get-Command ollama.exe -ErrorAction SilentlyContinue
    if (-not $command) { $command = Get-Command ollama -ErrorAction SilentlyContinue }
    if ($command) { return $command.Source }
    $candidates = @()
    if ($env:LOCALAPPDATA) { $candidates += (Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe') }
    if ($env:ProgramFiles) { $candidates += (Join-Path $env:ProgramFiles 'Ollama\ollama.exe') }
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    }
    return $null
}

function Start-MatrixOllamaIfNeeded {
    <# Reutiliza Ollama abierto o inicia el binario ya instalado, solo en loopback.
       No instala, descarga, elimina ni sustituye modelos. No modifica servicios
       Windows, PATH, firewall ni variables persistentes del usuario. #>
    param([ValidateRange(1, 120)][int]$TimeoutSeconds = 45)
    $providers = @(
        (Get-MatrixEnvValue -Key 'LLM_PROVIDER' -Default 'ollama'),
        (Get-MatrixEnvValue -Key 'LLM_DEEP_PROVIDER' -Default 'ollama'),
        (Get-MatrixEnvValue -Key 'LLM_EMBEDDING_PROVIDER' -Default 'ollama')
    )
    if ($providers -notcontains 'ollama') { return $true }
    try { $endpoint = [uri](Get-MatrixEnvValue -Key 'OLLAMA_BASE_URL' -Default 'http://127.0.0.1:11434') }
    catch { Write-Fail 'OLLAMA_BASE_URL invalida en .env.'; return $false }
    if (-not $endpoint.IsAbsoluteUri -or $endpoint.UserInfo -or $endpoint.Query -or $endpoint.Fragment -or
        $endpoint.AbsolutePath -ne '/' -or @('http', 'https') -notcontains $endpoint.Scheme) {
        Write-Fail 'OLLAMA_BASE_URL debe ser http(s)://host:puerto sin credenciales, ruta ni parametros.'
        return $false
    }
    if (-not $endpoint.IsLoopback) {
        Write-Warn 'El runtime configurado no es loopback; el preflight lo comprobara. No se inicia un Ollama local distinto.'
        return $true
    }
    if (Test-MatrixOllamaUp -Endpoint $endpoint) {
        Write-Ok 'Ollama ya responde; se reutiliza el proceso y los modelos existentes.'
        return $true
    }
    if ($endpoint.Scheme -ne 'http') {
        Write-Fail 'El endpoint HTTPS de Ollama no responde. Inicie su proxy TLS y runtime configurados.'
        return $false
    }
    $ollama = Find-MatrixOllama
    if (-not $ollama) {
        Write-Fail 'Ollama no responde y no se encontro su ejecutable. Abra la aplicacion Ollama instalada y repita.'
        return $false
    }
    New-MatrixRuntimeDirs
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmssfff'
    $outLog = Join-Path $script:LogDir "ollama-$stamp.log"
    $errLog = Join-Path $script:LogDir "ollama-$stamp.err.log"
    $previousHost = $env:OLLAMA_HOST
    $previousCloud = $env:OLLAMA_NO_CLOUD
    try {
        # Entorno exclusivo del hijo; impide heredar una escucha abierta a LAN.
        $env:OLLAMA_HOST = $endpoint.Authority
        $env:OLLAMA_NO_CLOUD = '1'
        Write-Step 'Iniciando el Ollama ya instalado para esta sesion...'
        $process = Start-Process -FilePath $ollama -ArgumentList @('serve') -WindowStyle Hidden `
            -RedirectStandardOutput $outLog -RedirectStandardError $errLog -PassThru
    }
    catch {
        Write-Fail 'No se pudo iniciar Ollama. Abra la aplicacion Ollama y repita el arranque.'
        return $false
    }
    finally {
        $env:OLLAMA_HOST = $previousHost
        $env:OLLAMA_NO_CLOUD = $previousCloud
    }
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        # Permite una carrera con la aplicacion Ollama del usuario sin duplicarla.
        if (Test-MatrixOllamaUp -Endpoint $endpoint) {
            Write-Ok 'Ollama listo; ahora se verifican los dos modelos locales configurados.'
            return $true
        }
        if ($process.HasExited) { break }
        Start-Sleep -Milliseconds 250
    }
    Write-Fail "Ollama no respondio en el plazo. Revise el log local $errLog y vuelva a intentar."
    return $false
}

function Find-WampMySql {
    <# Detecta una instalacion de WAMP para informar al operador. #>
    $directory = Get-Item -LiteralPath $script:MatrixRoot
    while ($null -ne $directory) {
        if (Test-Path -LiteralPath (Join-Path $directory.FullName 'wampmanager.exe')) {
            return $directory.FullName
        }
        $directory = $directory.Parent
    }
    foreach ($path in @("C:\wamp64\www", "C:\wamp\www")) {
        if (Test-Path $path) { return $path }
    }
    return $null
}

function New-MatrixRuntimeDirs {
    foreach ($dir in @($script:LogDir, (Join-Path $script:MatrixRoot "var\uploads"), (Join-Path $script:MatrixRoot "var\qdrant"), (Join-Path $script:MatrixRoot "reports\tests"))) {
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    }
}
