@echo off
title CodeMentor - AI Programming Learning Platform
cd /d "%~dp0"

echo =============================================================
echo   Starting CodeMentor AI Programming Learning Platform
echo =============================================================
echo.

where python >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Python is not installed or not in your system PATH!
    echo Please install Python 3.10+ from python.org and check "Add Python to PATH".
    pause
    exit /b 1
)

echo Opening browser at http://localhost:8000 ...
start "" cmd /c "timeout /t 2 /nobreak >nul && start http://localhost:8000"

echo Launching CodeMentor Web Server...
python server.py

if %ERRORLEVEL% neq 0 (
    echo.
    echo [NOTE] Server terminated with exit code %ERRORLEVEL%.
    pause
)
