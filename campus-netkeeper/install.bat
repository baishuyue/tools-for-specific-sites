@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -c "import sys" >nul 2>&1 && (set "PY=python") || (set "PY=py -3")
%PY% "deploy\windows_install.py"
echo.
pause
