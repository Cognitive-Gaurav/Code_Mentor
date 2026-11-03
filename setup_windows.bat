@echo off
setlocal
cd /d "%~dp0"

echo CodeMentor Windows setup
echo ========================

where python >nul 2>nul
if errorlevel 1 (
    echo ERROR: Python was not found on PATH.
    echo Install Python 3.10 or newer from python.org, enable Add Python to PATH, then rerun this script.
    exit /b 1
)

python -c "import sys; print('Python', sys.version.split()[0]); raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 (
    echo ERROR: CodeMentor requires Python 3.10 or newer.
    exit /b 1
)

if not exist ".env" (
    if exist ".env.example" (
        copy /Y ".env.example" ".env" >nul
        if errorlevel 1 (
            echo ERROR: Could not create .env from .env.example.
            exit /b 1
        )
        echo Created a credential-free .env file. Add provider credentials only if you choose to use a live AI provider.
    )
)

echo No third-party Python packages are required.
where gcc >nul 2>nul
if errorlevel 1 (echo Optional: GCC not found; native C execution will be unavailable.) else (echo Optional C compiler: GCC found.)
where g++ >nul 2>nul
if errorlevel 1 (echo Optional: G++ not found; native C++ execution will be unavailable.) else (echo Optional C++ compiler: G++ found.)
where javac >nul 2>nul
if errorlevel 1 (echo Optional: JDK not found; Java execution will be unavailable.) else (echo Optional Java compiler: javac found.)
where java >nul 2>nul
if errorlevel 1 (echo Optional: Java runtime not found; Java execution will be unavailable.) else (echo Optional Java runtime: java found.)

echo.
echo Setup checks complete. No database was created or modified by this script.
echo Run start_windows.bat to launch CodeMentor.
exit /b 0
