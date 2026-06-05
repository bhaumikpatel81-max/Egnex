@echo off
title Egnex Server
color 0A
echo ========================================
echo   EGNEX - One Click Hire
echo   Starting server on port 8000...
echo ========================================
echo.
cd /d "%~dp0"

:: Activate virtual environment if present
if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
) else if exist ".venv\Scripts\activate.bat" (
    call .venv\Scripts\activate.bat
)

echo Server: http://localhost:8000
echo Press Ctrl+C to stop.
echo.

py -m uvicorn backend.app.main:app --port 8000 --reload
pause
