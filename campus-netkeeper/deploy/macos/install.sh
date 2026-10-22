#!/bin/sh
# campus-netkeeper · macOS 安装脚本
#
# 使用 launchd LaunchAgent，以 StartInterval 周期触发 tick。
# 不需要 sudo，也不会常驻进程。
#
# 用法：  sh deploy/macos/install.sh
# 卸载：  sh deploy/macos/uninstall.sh

set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
PY=$(command -v python3 || true)
LABEL="com.campusnetkeeper.tick"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ -z "$PY" ]; then
    echo "错误：找不到 python3。可执行 xcode-select --install 安装命令行工具。" >&2
    exit 1
fi

if [ ! -f "$ROOT/config.json" ]; then
    echo "错误：$ROOT/config.json 不存在。请先运行：$PY $ROOT/main.py init" >&2
    exit 1
fi

INTERVAL=$("$PY" - "$ROOT/config.json" <<'EOF'
import json, sys
try:
    value = int(json.load(open(sys.argv[1], encoding="utf-8")).get("interval", 120))
except Exception:
    value = 120
print(max(60, value))
EOF
)

echo "==================================================================="
echo "campus-netkeeper · macOS 安装"
echo "==================================================================="
echo "项目目录 : $ROOT"
echo "解释器   : $PY"
echo "检测间隔 : ${INTERVAL} 秒"
echo

mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/logs"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PY</string>
        <string>$ROOT/main.py</string>
        <string>tick</string>
        <string>--quiet</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$ROOT</string>
    <key>RunAtLoad</key>
    <true/>
    <key>StartInterval</key>
    <integer>$INTERVAL</integer>
    <key>StandardOutPath</key>
    <string>$ROOT/logs/launchd.out.log</string>
    <key>StandardErrorPath</key>
    <string>$ROOT/logs/launchd.err.log</string>
    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
launchctl start "$LABEL" 2>/dev/null || true

echo "  [OK]   已加载 LaunchAgent：$PLIST"
echo "  -> 登录后立即执行一次，之后每 ${INTERVAL} 秒检查一次"
echo "  -> 每次执行约 1~3 秒后退出，不常驻内存"
echo
echo "常用命令："
echo "  launchctl list | grep campusnetkeeper   查看是否已加载"
echo "  $PY $ROOT/main.py status                查看运行状态"
echo "==================================================================="
