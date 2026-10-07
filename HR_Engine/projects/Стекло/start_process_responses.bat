@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

title HR Engine - Разбор откликов Playwright

set "CONFIG_FILE=%~dp0config.json"
set "SCRIPT=%~dp0..\..\process_responses_playwright.py"

echo ============================================================
echo HR Engine - Разбор откликов через Playwright
echo ============================================================
echo.

if not exist "%CONFIG_FILE%" (
    echo ОШИБКА: не найден config.json
    echo %CONFIG_FILE%
    pause
    exit /b 1
)

if not exist "%SCRIPT%" (
    echo ОШИБКА: не найден process_responses_playwright.py
    echo %SCRIPT%
    echo.
    echo Положи Python-файл в C:\HR_Engine\
    pause
    exit /b 1
)

python "%SCRIPT%" "%CONFIG_FILE%"
set "EXIT_CODE=%ERRORLEVEL%"

echo.
echo ============================================================
echo Код завершения: %EXIT_CODE%
echo ============================================================
pause

endlocal & exit /b %EXIT_CODE%
