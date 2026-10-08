@echo off
REM Creado por Aldo Garcia.
REM Detiene el backend de Matrix RH. Solo toca procesos de este proyecto.
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title Matrix RH - Detener

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0windows\Stop-MatrixRH.ps1" %*
set "MATRIX_EXIT=%ERRORLEVEL%"
echo.
pause
exit /b %MATRIX_EXIT%
