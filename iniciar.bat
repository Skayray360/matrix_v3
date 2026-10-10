@echo off
REM Creado por Aldo Garcia.
setlocal
cd /d "%~dp0"
title Matrix RH - iniciar
where powershell.exe >nul 2>&1
if errorlevel 1 (
    echo [FALLA] Se requiere Windows PowerShell 5.1 x64.
    pause
    exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0backend\scripts\windows\MatrixRH.ps1" -Action iniciar %*
set "MATRIX_EXIT=%ERRORLEVEL%"
if not "%MATRIX_EXIT%"=="0" echo [FALLA] La operacion termino con codigo %MATRIX_EXIT%.
pause
exit /b %MATRIX_EXIT%
