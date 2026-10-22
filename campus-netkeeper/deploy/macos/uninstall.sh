#!/bin/sh
# campus-netkeeper · macOS 卸载脚本

set -eu

LABEL="com.campusnetkeeper.tick"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

echo "campus-netkeeper · macOS 卸载"

launchctl unload "$PLIST" 2>/dev/null || true
rm -f "$PLIST"
echo "  [OK]   已移除 LaunchAgent"
echo "卸载完成。配置、日志与状态仍保留在项目目录。"
