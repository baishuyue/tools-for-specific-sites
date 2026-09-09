@echo off
setlocal
cd /d "%~dp0"

if exist "%LocalAppData%\Programs\Python\Python312\python.exe" (
    "%LocalAppData%\Programs\Python\Python312\python.exe" "%~dp0ulearning_standalone.py"
    if errorlevel 1 pause
    exit /b
)

where python >nul 2>nul
if not errorlevel 1 (
    python "%~dp0ulearning_standalone.py"
) else (
    py -3.12 "%~dp0ulearning_standalone.py"
)

if errorlevel 1 pause
