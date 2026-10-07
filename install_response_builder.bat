@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONUTF8=1"
if not exist "%~dp0_shared\response_venv\Scripts\python.exe" (
  py -3.12 -m venv "%~dp0_shared\response_venv"
  if errorlevel 1 goto failed
)
"%~dp0_shared\response_venv\Scripts\python.exe" -m pip install -r "%~dp0response_builder\requirements.txt"
if errorlevel 1 goto failed
"%~dp0_shared\response_venv\Scripts\python.exe" -m pip check
if errorlevel 1 goto failed
"%~dp0_shared\response_venv\Scripts\python.exe" -m playwright install chromium
if errorlevel 1 goto failed
echo Установка завершена. Запустите create_response_project.bat.
pause
exit /b 0
:failed
echo Установка не завершена. Проверьте Python 3.12, сеть и сообщения выше.
pause
exit /b 1
