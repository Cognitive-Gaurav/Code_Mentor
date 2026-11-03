@echo off
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo ERROR: Python was not found on PATH. Run setup_windows.bat first.
    pause
    exit /b 1
)

echo Starting CodeMentor. Keep this window open while using the application.
echo The server will print its listening URL below.
echo Press Ctrl+C to stop the server.
echo.
python server.py
set "server_exit=%ERRORLEVEL%"
if not "%server_exit%"=="0" (
    echo.
    echo CodeMentor stopped with exit code %server_exit%.
    pause
)
exit /b %server_exit%
