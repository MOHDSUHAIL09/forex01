@echo off
title ForexBot Server Launcher
cd /d "%~dp0"

echo ====================================================
echo           ForexBot Windows Server Launcher
echo ====================================================
echo.

:: Check Python installation
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed or not in PATH!
    echo Please install Python 3.10+ and check "Add Python to PATH".
    pause
    exit /b 1
)

:: Install/verify requirements
echo [1/2] Checking dependencies...
pip install -r requirements.txt --quiet

:: Start Server on 0.0.0.0:5000
echo [2/2] Starting ForexBot Server on http://0.0.0.0:5000 ...
echo.
echo Dashboard URL: http://localhost:5000
echo.
python -u server.py

pause
