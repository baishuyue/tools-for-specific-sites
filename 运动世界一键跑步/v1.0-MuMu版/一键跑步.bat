@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 运动世界校园 - 一键跑步

rem 优先使用 py 启动器，其次 python
where py >nul 2>nul
if %errorlevel%==0 (
    py -c "import Crypto" >nul 2>nul
    if errorlevel 1 (
        echo 首次运行：正在安装依赖 pycryptodome ...
        py -m pip install pycryptodome
    )
    py sport_run.py gui
    goto :end
)

where python >nul 2>nul
if %errorlevel%==0 (
    python -c "import Crypto" >nul 2>nul
    if errorlevel 1 (
        echo 首次运行：正在安装依赖 pycryptodome ...
        python -m pip install pycryptodome
    )
    python sport_run.py gui
    goto :end
)

echo 未检测到 Python，请先安装 Python 3.10+（勾选 Add to PATH）。
echo 下载地址 https://www.python.org/downloads/
pause
:end
if errorlevel 1 pause
