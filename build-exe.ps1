# 打包成单文件 exe（可选）。产物：dist\一键跑步.exe
# 用法：powershell -ExecutionPolicy Bypass -File build-exe.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $MyInvocation.MyCommand.Path)

Write-Host "[1/3] 安装 pyinstaller ..."
python -m pip install --upgrade pyinstaller

Write-Host "[2/3] 打包 ..."
python -m PyInstaller --noconfirm --clean `
    --name "一键跑步" `
    --onefile `
    --windowed `
    --add-data "data;data" `
    --hidden-import sportwin.gui `
    --hidden-import sportwin.selftest `
    sport_run.py

Write-Host "[3/3] 完成：dist\一键跑步.exe"
Write-Host "提示：exe 首次运行会在同目录生成 config.json / credentials.bin / logs。"
