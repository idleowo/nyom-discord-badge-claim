@echo off
setlocal
title Discord Badge Spoofer

:: Navigate to script folder
cd /d "%~dp0"

:: Check Python installation
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed or not found in PATH!
    echo Please install Python (3.10+) and ensure "Add Python to PATH" is checked.
    echo Download: https://www.python.org/
    echo.
    pause
    exit /b 1
)

:: Install requirements
if exist "requirements.txt" (
    echo [*] Installing dependencies from requirements.txt...
    python -m pip install -r requirements.txt
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to install dependencies.
        echo.
        pause
        exit /b 1
    )
    echo [*] Dependencies are up to date.
    echo.
)

:: Run main.py
echo [*] Starting main.py...
echo.
python main.py

if %errorlevel% neq 0 (
    echo.
    echo [!] Program exited with error code %errorlevel%.
    pause
)

endlocal