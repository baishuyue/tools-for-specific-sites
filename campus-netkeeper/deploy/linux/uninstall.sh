#!/bin/sh
# campus-netkeeper · Linux 卸载脚本

set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

echo "campus-netkeeper · Linux 卸载"

if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
    systemctl --user disable --now campus-netkeeper.timer 2>/dev/null || true
    rm -f "$UNIT_DIR/campus-netkeeper.service" "$UNIT_DIR/campus-netkeeper.timer"
    systemctl --user daemon-reload
    echo "  [OK]   已移除 systemd timer"
fi

if command -v crontab >/dev/null 2>&1; then
    EXISTING=$(crontab -l 2>/dev/null || true)
    if printf '%s\n' "$EXISTING" | grep -q "campus-netkeeper"; then
        printf '%s\n' "$EXISTING" | grep -v "campus-netkeeper" | grep -v "main.py tick" | crontab -
        echo "  [OK]   已移除 crontab 条目"
    fi
fi

echo "卸载完成。配置、日志与状态仍保留在 $ROOT"
