@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" launch.py
) else (
    where py >nul 2>nul
    if not errorlevel 1 (
        py -3 launch.py
    ) else (
        python launch.py
    )
)
if errorlevel 1 (
    echo.
    echo Please install Python 3.11 or newer and try again.
    pause
)
