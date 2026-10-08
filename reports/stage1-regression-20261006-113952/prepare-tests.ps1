$ErrorActionPreference = 'Stop'
$reportRoot = $PSScriptRoot
$projectRoot = Split-Path (Split-Path $reportRoot -Parent) -Parent
$env:UV_PROJECT_ENVIRONMENT = Join-Path $reportRoot 'test-env'
$env:UV_CACHE_DIR = Join-Path $reportRoot 'uv-cache'
$env:UV_PYTHON_DOWNLOADS = 'never'
$arguments = @('sync', '--project', (Join-Path $projectRoot 'backend'), '--frozen', '--extra', 'dev', '--no-install-project', '--python', 'C:\Users\AH015398\AppData\Local\Programs\Python\Python312\python.exe', '--no-python-downloads', '--link-mode', 'copy', '--no-progress')
@{command = 'uv'; arguments = $arguments; environment = @{UV_PROJECT_ENVIRONMENT = $env:UV_PROJECT_ENVIRONMENT; UV_CACHE_DIR = $env:UV_CACHE_DIR; UV_PYTHON_DOWNLOADS = $env:UV_PYTHON_DOWNLOADS}} | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $reportRoot 'preparation-command.json') -Encoding utf8
& uv @arguments 2>&1 | Tee-Object -FilePath (Join-Path $reportRoot 'preparation.log')
$resultCode = $LASTEXITCODE
@{exit_code = $resultCode} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $reportRoot 'preparation-result.json') -Encoding utf8
exit $resultCode
