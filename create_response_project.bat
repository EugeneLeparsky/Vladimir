@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONDONTWRITEBYTECODE=1"
if exist "%~dp0_shared\response_venv\Scripts\python.exe" (
  "%~dp0_shared\response_venv\Scripts\python.exe" -m response_builder.create_project --root "%~dp0." %*
) else (
  py -3.12 -m response_builder.create_project --root "%~dp0." %*
)
set "RC=%ERRORLEVEL%"
echo Код завершения: %RC%
pause
exit /b %RC%
