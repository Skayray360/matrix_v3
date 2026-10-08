@echo off
REM Creado por Aldo Garcia.
REM ---------------------------------------------------------------------------
REM Arranque de Matrix RH por doble clic.
REM Ejecuta preflight, levanta el backend, espera /health y /ready, sirve el
REM frontend same-origin y abre el navegador. Toda la logica vive en
REM windows\Start-MatrixRH.ps1.
REM ---------------------------------------------------------------------------
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title Matrix RH - Arranque

if not exist "%~dp0.venv\Scripts\python.exe" (
    echo [FAIL] Matrix RH no esta instalado en este equipo.
    echo        Ejecute primero INSTALAR_MATRIX_RH.bat
    echo.
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0windows\Start-MatrixRH.ps1" %*
set "MATRIX_EXIT=%ERRORLEVEL%"

if not "%MATRIX_EXIT%"=="0" (
    echo.
    echo ============================================================
    echo   MATRIX RH NO PUDO ARRANCAR ^(codigo %MATRIX_EXIT%^)
    echo   Ejecute DIAGNOSTICO_MATRIX_RH.bat para ver el detalle.
    echo ============================================================
    echo.
    pause
)
exit /b %MATRIX_EXIT%
