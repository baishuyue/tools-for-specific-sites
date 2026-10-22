#!/bin/sh
# campus-netkeeper · Linux 安装脚本
#
# 优先使用 systemd 用户级 timer（不需要 root，也不会常驻进程）；
# 没有 systemd 时退回 crontab。
#
# 用法：  sh deploy/linux/install.sh
# 卸载：  sh deploy/linux/uninstall.sh

set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
PY=$(command -v python3 || command -v python || true)
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
SERVICE="campus-netkeeper.service"
TIMER="campus-netkeeper.timer"

if [ -z "$PY" ]; then
    echo "错误：找不到 python3，请先安装 Python 3.8 或更高版本。" >&2
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
print(max(1, value // 60 or 1))
EOF
)

echo "==================================================================="
echo "campus-netkeeper · Linux 安装"
echo "==================================================================="
echo "项目目录 : $ROOT"
echo "解释器   : $PY"
echo "检测间隔 : ${INTERVAL} 分钟"
echo

if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
    echo "使用 systemd 用户级 timer（空闲时不占内存）…"
    mkdir -p "$UNIT_DIR"

    cat > "$UNIT_DIR/$SERVICE" <<EOF
[Unit]
Description=Campus network auto login (one-shot)
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$ROOT
ExecStart=$PY $ROOT/main.py tick --quiet
# 单次执行通常 1~3 秒，给足余量
TimeoutStartSec=120
EOF

    cat > "$UNIT_DIR/$TIMER" <<EOF
[Unit]
Description=Periodic campus network check

[Timer]
OnBootSec=30s
OnUnitActiveSec=${INTERVAL}min
AccuracySec=15s
Persistent=true
Unit=$SERVICE

[Install]
WantedBy=timers.target
EOF

    systemctl --user daemon-reload
    systemctl --user enable --now "$TIMER"
    systemctl --user start "$SERVICE" || true

    echo
    echo "  [OK]   已启用 $TIMER"
    echo "  -> 开机后 30 秒首次检查，之后每 ${INTERVAL} 分钟一次"
    echo "  -> 每次执行约 1~3 秒后退出，不常驻内存"
    echo
    echo "常用命令："
    echo "  systemctl --user status $TIMER      查看定时器状态"
    echo "  systemctl --user list-timers        查看下次触发时间"
    echo "  journalctl --user -u $SERVICE -n 50 查看最近日志"
    echo
    echo "提示：如果希望「未登录桌面」时也生效，执行一次："
    echo "  loginctl enable-linger \$USER"
else
    echo "未检测到可用的 systemd，改用 crontab…"
    EXISTING=$(crontab -l 2>/dev/null || true)
    if printf '%s\n' "$EXISTING" | grep -q "campus-netkeeper"; then
        echo "  [跳过] crontab 中已存在 campus-netkeeper 条目"
    else
        {
            printf '%s\n' "$EXISTING"
            echo "# campus-netkeeper 校园网自动认证"
            echo "*/${INTERVAL} * * * * cd $ROOT && $PY main.py tick --quiet >> logs/cron.log 2>&1"
        } | crontab -
        echo "  [OK]   已写入 crontab"
    fi
fi

echo
echo "验证：$PY $ROOT/main.py status"
echo "==================================================================="
