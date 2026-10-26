@echo off
chcp 65001 >nul
setlocal

REM ============================================================
REM  优学院签到助手 —— 一键推送到 GitHub 并触发 APK 在线构建
REM
REM  用法：
REM    1. 先在 GitHub 网页端新建一个仓库（建议私有）
REM       例如：https://github.com/你的用户名/ulearning-dgut
REM    2. 把下面的 REPO_URL 改成你的仓库地址
REM    3. 双击本脚本，或在命令行执行 push-and-build.bat
REM    4. 推送成功后打开仓库的 Actions 页面下载 APK
REM ============================================================

set REPO_URL=https://github.com/你的用户名/ulearning-dgut.git

echo.
echo ============================================================
echo   目标仓库: %REPO_URL%
echo ============================================================
echo.

if "%REPO_URL%"=="https://github.com/你的用户名/ulearning-dgut.git" (
    echo [错误] 请先用记事本打开本脚本，把 REPO_URL 改成你自己的仓库地址。
    echo.
    pause
    exit /b 1
)

cd /d "%~dp0"

echo [1/4] 检查 git 仓库...
if not exist ".git" (
    git init
)

echo [2/4] 设置分支与远程...
git branch -M main
git remote remove origin >nul 2>&1
git remote add origin %REPO_URL%

echo [3/4] 提交改动...
git add -A
git commit -m "优学院签到助手 Android 版：Kivy 重制 UI + 内置中文字体 + Buildozer 打包配置" >nul 2>&1

echo [4/4] 推送到 GitHub...
echo      （首次会要求登录：用浏览器授权，或输入 GitHub 用户名 + Personal Access Token）
git push -u origin main
if errorlevel 1 (
    echo.
    echo [失败] 推送失败。常见原因：
    echo   - 仓库地址填错或仓库不存在
    echo   - 未登录 GitHub（HTTPS 需要 Personal Access Token 作为密码）
    echo   - 网络问题
    echo.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   推送成功！
echo.
echo   下一步：
echo   1. 打开 %REPO_URL:.git=%
echo   2. 点击顶部的 [Actions] 标签
echo   3. 选择左侧 "Build Ulearning Android APK"
echo   4. 构建完成后，在该次运行页面底部下载
echo      Artifact: ulearning-dgut-debug-apk
echo.
echo   首次构建约 20~40 分钟（需下载 Android SDK/NDK）
echo   之后命中缓存约 8~15 分钟
echo ============================================================
echo.
pause
