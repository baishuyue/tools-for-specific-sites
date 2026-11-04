@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 运动世界校园 - 一键跑步（纯 Windows）

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo.
    echo  未检测到 Python 3.9+。请安装 https://www.python.org/downloads/ 并勾选 Add to PATH，
    echo  或直接使用已打包的 一键跑步.exe。
    echo.
    pause
    exit /b 1
)

%PY% -c "import sys;sys.exit(0 if sys.version_info>=(3,9) else 1)" >nul 2>nul
if errorlevel 1 (
    echo  当前 Python 版本过低，需要 3.9 及以上。
    pause
    exit /b 1
)

if "%~1"=="" (
    %PY% sport_run.py gui
) else (
    %PY% sport_run.py %*
)
if errorlevel 1 pause
