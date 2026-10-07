@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

title HR Engine - DEBUG разбора откликов

set "PROJECT_DIR=%~dp0"
set "CONFIG_FILE=%~dp0config.json"
set "AUTH_FILE=%~dp0rabota_auth.json"
set "SCRIPT=%~dp0..\..\process_responses_playwright.py"
set "GOOGLE_KEY=%~dp0..\..\google_credentials.json"

echo ============================================================
echo HR Engine - диагностика запуска
echo ============================================================
echo.
echo Папка проекта:
echo %PROJECT_DIR%
echo.
echo config.json:
echo %CONFIG_FILE%
echo.
echo rabota_auth.json:
echo %AUTH_FILE%
echo.
echo Python-скрипт:
echo %SCRIPT%
echo.
echo Google-ключ:
echo %GOOGLE_KEY%
echo.

echo [1/5] Проверяем Python...
where python
if errorlevel 1 (
    echo.
    echo ОШИБКА: команда python не найдена.
    goto KEEP_OPEN
)
python --version
echo.

echo [2/5] Проверяем config.json...
if not exist "%CONFIG_FILE%" (
    echo ОШИБКА: config.json не найден.
    goto KEEP_OPEN
)
echo OK
echo.

echo [3/5] Проверяем rabota_auth.json...
if not exist "%AUTH_FILE%" (
    echo ОШИБКА: rabota_auth.json не найден.
    goto KEEP_OPEN
)
echo OK
echo.

echo [4/5] Проверяем основной Python-файл...
if not exist "%SCRIPT%" (
    echo ОШИБКА: process_responses_playwright.py не найден.
    echo Ожидался здесь:
    echo %SCRIPT%
    goto KEEP_OPEN
)
echo OK
echo.

echo [5/5] Проверяем google_credentials.json...
if not exist "%GOOGLE_KEY%" (
    echo ОШИБКА: google_credentials.json не найден.
    echo Ожидался здесь:
    echo %GOOGLE_KEY%
    goto KEEP_OPEN
)
echo OK
echo.

echo ============================================================
echo Все файлы найдены. Запускаем Python.
echo Консоль останется открытой после завершения.
echo ============================================================
echo.

python -u "%SCRIPT%" "%CONFIG_FILE%"

echo.
echo ============================================================
echo Python завершился.
echo Код завершения: %ERRORLEVEL%
echo ============================================================
echo.

:KEEP_OPEN
echo.
echo Окно оставлено открытым для диагностики.
echo Скопируй мне весь текст ошибки выше.
echo.
cmd /k

endlocal
