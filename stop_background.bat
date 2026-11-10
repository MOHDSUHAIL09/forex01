@echo off
title Stop ForexBot Server
echo Stopping ForexBot server processes...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :5000') do (
    taskkill /F /PID %%a >nul 2>&1
)
echo ForexBot server on port 5000 stopped.
pause
