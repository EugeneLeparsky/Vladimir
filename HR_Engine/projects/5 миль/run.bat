@echo off
chcp 65001 >nul
title HR Parser — 5 миль

cd /d "C:\HR_Engine"

py -3.12 core_parser.py "C:\HR_Engine\projects\5 миль\config.json"

echo.
echo ==============================================================
echo Код завершения: %errorlevel%
echo ==============================================================
pause
