@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist "logs\netkeeper.log" (
  echo 还没有日志。请先运行一次 check_status.bat 或 login_now.bat。
  echo.
  pause
  exit /b 0
)
powershell -NoProfile -Command "Get-Content -LiteralPath 'logs\netkeeper.log' -Tail 40 -Encoding UTF8"
echo.
pause
