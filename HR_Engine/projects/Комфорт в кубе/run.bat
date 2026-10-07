@echo off
chcp 65001 >nul
title HR Parser — Комфорт в кубе

cd /d "C:\HR_Engine"

py -3.12 core_parser.py "C:\HR_Engine\projects\Комфорт в кубе\config.json"

echo.
echo ==============================================================
echo Код завершения: %errorlevel%
echo ==============================================================
pause
