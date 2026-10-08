@echo off
REM Creado por Aldo Garcia.
REM ---------------------------------------------------------------------------
REM Instalador UNICO de Matrix RH: instala E inicializa el sistema.
REM
REM   1) Instalacion: delega en windows\Install-MatrixRH.ps1
REM      (Python 3.12, Ollama y modelos, .env, venv, dependencias, base de
REM       datos, migraciones, seed, preflight e ingesta inicial).
REM   2) Inicializacion: si la instalacion termina en 0, arranca el backend,
REM      espera /health y /ready, sirve el frontend same-origin y abre el
REM      navegador (windows\Start-MatrixRH.ps1).
REM
REM Con un solo doble clic el sistema queda instalado Y en ejecucion.
REM Para arranques posteriores use INICIAR_MATRIX_RH.bat.
REM
REM Parametros opcionales (se pasan al instalador):
REM   -WithAutostart   registra el arranque al iniciar sesion de Windows.
REM   -RebuildFrontend recompila la interfaz (requiere Node/Corepack).
REM   -SkipFrontend    conserva la interfaz incluida (comportamiento default).
REM   -SkipIngest      no ejecuta la ingesta inicial del conocimiento.
REM ---------------------------------------------------------------------------
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title Matrix RH - Instalacion e inicializacion

where powershell >nul 2>&1
if errorlevel 1 (
    echo [FAIL] No se encontro PowerShell en este equipo.
    echo        Matrix RH requiere Windows con PowerShell 5.1 o superior.
    pause
    exit /b 1
)

REM --- Paso 1: instalacion -----------------------------------------------------
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0windows\Install-MatrixRH.ps1" %*
set "MATRIX_EXIT=%ERRORLEVEL%"

if not "%MATRIX_EXIT%"=="0" (
    echo.
    echo ============================================================
    echo   LA INSTALACION FALLO ^(codigo %MATRIX_EXIT%^)
    echo   Ejecute DIAGNOSTICO_MATRIX_RH.bat para ver el detalle.
    echo ============================================================
    echo.
    pause
    exit /b %MATRIX_EXIT%
)

echo.
echo ============================================================
echo   INSTALACION COMPLETADA - inicializando el sistema...
echo ============================================================
echo.

REM --- Paso 2: inicializacion (arranque) --------------------------------------
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0windows\Start-MatrixRH.ps1"
set "MATRIX_EXIT=%ERRORLEVEL%"

echo.
if "%MATRIX_EXIT%"=="0" (
    echo ============================================================
    echo   MATRIX RH INSTALADO Y EN EJECUCION
    echo   Arranques posteriores: INICIAR_MATRIX_RH.bat
    echo   Detener:               DETENER_MATRIX_RH.bat
    echo ============================================================
) else (
    echo ============================================================
    echo   INSTALADO, PERO NO PUDO ARRANCAR ^(codigo %MATRIX_EXIT%^)
    echo   Ejecute DIAGNOSTICO_MATRIX_RH.bat para ver el detalle.
    echo ============================================================
)
echo.
pause
exit /b %MATRIX_EXIT%
