@echo off
setlocal

title HR Engine AI Filter

echo ============================================================
echo HR Engine AI Filter
echo ============================================================
echo.

echo [1/3] Checking Ollama...
ollama list >nul 2>&1
if %errorlevel%==0 goto OLLAMA_READY

echo Ollama is not running. Starting Ollama...
start "" /min cmd /c "ollama serve >nul 2>&1"

set /a TRY_COUNT=0

:WAIT_OLLAMA
set /a TRY_COUNT+=1
timeout /t 1 /nobreak >nul
ollama list >nul 2>&1
if %errorlevel%==0 goto OLLAMA_READY
if %TRY_COUNT% LSS 20 goto WAIT_OLLAMA

echo.
echo ERROR: Ollama did not start within 20 seconds.
echo Run "ollama list" manually to check it.
echo.
pause
exit /b 1

:OLLAMA_READY
echo Ollama is ready.
echo.

echo [2/3] Checking files...
if not exist "%~dp0config.json" (
    echo ERROR: config.json was not found next to this BAT file.
    echo Expected: %~dp0config.json
    pause
    exit /b 1
)

if not exist "%~dp0..\..\ai_filter.py" (
    echo ERROR: ai_filter.py was not found in HR_Engine root.
    echo Expected: %~dp0..\..\ai_filter.py
    pause
    exit /b 1
)

echo Files are present.
echo.

echo [3/3] Starting AI filter...
python "%~dp0..\..\ai_filter.py" "%~dp0config.json"

echo.
echo ============================================================
echo Finished.
echo ============================================================
pause

endlocal
