@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

title HR Engine - Response Processor Debug

set "PYTHONUTF8=1"
set "CONFIG_FILE=%~dp0config.json"
set "AUTH_FILE=%~dp0rabota_auth.json"
set "SCRIPT=%~dp0..\..\process_responses_playwright.py"
set "GOOGLE_KEY=%~dp0..\..\google_credentials.json"

echo ============================================================
echo HR Engine - DEBUG launcher
echo ============================================================
echo.
echo Project dir:
echo %~dp0
echo.
echo CONFIG_FILE:
echo %CONFIG_FILE%
echo.
echo AUTH_FILE:
echo %AUTH_FILE%
echo.
echo SCRIPT:
echo %SCRIPT%
echo.
echo GOOGLE_KEY:
echo %GOOGLE_KEY%
echo.

echo [1/5] Checking Python...
where python
if errorlevel 1 goto ERR_PYTHON
python --version
echo.

echo [2/5] Checking config.json...
if not exist "%CONFIG_FILE%" goto ERR_CONFIG
echo OK
echo.

echo [3/5] Checking rabota_auth.json...
if not exist "%AUTH_FILE%" goto ERR_AUTH
echo OK
echo.

echo [4/5] Checking Python script...
if not exist "%SCRIPT%" goto ERR_SCRIPT
echo OK
echo.

echo [5/5] Checking google_credentials.json...
if not exist "%GOOGLE_KEY%" goto ERR_GOOGLE
echo OK
echo.

echo ============================================================
echo Starting Python...
echo ============================================================
echo.

python -u "%SCRIPT%" "%CONFIG_FILE%"

echo.
echo ============================================================
echo Python finished.
echo Exit code: %ERRORLEVEL%
echo ============================================================
echo.
pause
exit /b

:ERR_PYTHON
echo ERROR: python command not found.
goto END

:ERR_CONFIG
echo ERROR: config.json not found:
echo %CONFIG_FILE%
goto END

:ERR_AUTH
echo ERROR: rabota_auth.json not found:
echo %AUTH_FILE%
goto END

:ERR_SCRIPT
echo ERROR: process_responses_playwright.py not found:
echo %SCRIPT%
goto END

:ERR_GOOGLE
echo ERROR: google_credentials.json not found:
echo %GOOGLE_KEY%
goto END

:END
echo.
echo Copy the full error text from this window.
echo.
pause
exit /b 1
