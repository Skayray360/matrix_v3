@echo off
REM Creado por Aldo Garcia.
REM ---------------------------------------------------------------------------
REM Diagnostico de solo lectura: revisa Python, venv, Ollama, modelos, embeddings,
REM MySQL, Qdrant, frontend, backend, puertos, migraciones, usuarios de prueba,
REM scheduler, estado de Entra ID, conectores externos y logs recientes.
REM Sin parametros no modifica datos. -ProbarModelos ejecuta una prueba
REM sintetica activa y guarda su informe local (con Matrix detenido).
REM ---------------------------------------------------------------------------
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title Matrix RH - Diagnostico

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0windows\Diagnose-MatrixRH.ps1" %*
set "MATRIX_EXIT=%ERRORLEVEL%"
echo.
pause
exit /b %MATRIX_EXIT%
