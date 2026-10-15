[app]

# 应用信息
title = 签到器
package.name = ulearningdgut
package.domain = edu.dgut

# 源码目录与打包文件类型（含字体 ttf / 图标 png jpg）
source.dir = .
source.include_exts = py,png,jpg,jpeg,ttf,txt,json,md
# 关键：必须排除构建产物目录，否则 p4a 会用 hostpython3 编译 build/ 下
# 的 CPython 源码（含 Python2 语法文件）导致打包失败
source.exclude_dirs = build,bin,.git,.github,.buildozer,__pycache__,.venv
source.exclude_exts = pyc,pyo,pyd,so,o,a,log,spec.orig
source.exclude_patterns = */tests/*,*/test/*,*/*_test.py,*/setup.py

version = 1.2.1

# 依赖：与原 APK 一致（Python 3.11 / Kivy 2.3.0 / requests）
# 注意：python-for-android 在 3.11 上会应用 CPython 官方补丁，兼容性最好
requirements = python3==3.11.9,hostpython3==3.11.9,kivy==2.3.0,requests,urllib3,certifi,charset-normalizer,idna

# 图标与启动画面（从原 APK 提取，保持一致）
icon.filename = %(source.dir)s/icon.png
presplash.filename = %(source.dir)s/presplash.jpg

# 竖屏、有窗口、最小 SDK 21（与原 APK 的 p4a_env_vars 一致）
orientation = portrait
fullscreen = 0
android.archs = arm64-v8a, armeabi-v7a
android.minapi = 21
android.api = 31
android.ndk = 25b
android.accept_sdk_license = True
android.allow_backup = False
android.permissions = INTERNET,ACCESS_NETWORK_STATE
android.non_included_plugins =

# 日志
log_level = 2

#
# Windows 构建注意：Buildozer 不支持在 Windows 原生构建 Android SDK/NDK，
# 本项目通过 GitHub Actions 在线构建（见 .github/workflows/build-apk.yml）。
#

[buildozer]

# 构建输出目录
build_dir = build

# 建议在 CI 中开启并行
log_level = 2
warn_on_root = 1

# build: 626169736875797565-2026
