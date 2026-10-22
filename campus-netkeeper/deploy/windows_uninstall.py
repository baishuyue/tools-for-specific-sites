#!/usr/bin/env python3
"""Windows 卸载器（由 uninstall.bat 调用）。

只移除「开机自启」相关的注册项，保留配置文件与日志，方便重新安装
或换账号后继续使用。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RECORD = ROOT / ".installed.json"
LAUNCHER = ROOT / "_tick.cmd"

DEFAULT_TASKS = ["CampusNetkeeper-Logon", "CampusNetkeeper-Tick"]


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", **kwargs)
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(cmd, 1, "", f"{type(exc).__name__}: {exc}")


def _powershell(script: str, env=None) -> str:
    result = _run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script], env=env
    )
    return result.stdout or ""


def _stop_daemon() -> int:
    """结束本项目的常驻进程（只杀命令行匹配本目录的进程）。"""
    script = r"""
$root = $env:NK_ROOT
$found = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine.Contains($root) -and $_.CommandLine.Contains('main.py') }
$n = 0
foreach ($p in $found) {
    try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop; $n++ } catch {}
}
Write-Output $n
"""
    env = dict(os.environ)
    env["NK_ROOT"] = str(ROOT)
    output = _powershell(script, env=env)
    try:
        return int((output or "0").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return 0


def main() -> int:
    print("=" * 60)
    print("campus-netkeeper · Windows 卸载")
    print("=" * 60)

    tasks = list(DEFAULT_TASKS)
    if RECORD.exists():
        try:
            tasks = json.loads(RECORD.read_text(encoding="utf-8")).get("tasks") or tasks
        except (OSError, ValueError):
            pass

    removed = 0
    for name in tasks:
        result = _run(["schtasks", "/Delete", "/TN", name, "/F"])
        if result.returncode == 0:
            print(f"  [OK]   已删除计划任务 {name}")
            removed += 1
        elif "找不到" in (result.stderr or "") or "cannot find" in (result.stderr or "").lower():
            print(f"  [跳过] 计划任务 {name} 不存在")
        else:
            print(f"  [提示] 删除 {name} 未成功：{(result.stderr or '').strip()[:120]}")

    script = r"""
$startup = [Environment]::GetFolderPath('Startup')
$link = Join-Path $startup 'Campus Netkeeper.lnk'
if (Test-Path $link) { Remove-Item $link -Force; Write-Output 'OK' } else { Write-Output 'NONE' }
"""
    result = _powershell(script)
    if "OK" in result:
        print("  [OK]   已删除启动文件夹快捷方式")
        removed += 1
    else:
        print("  [跳过] 启动文件夹没有快捷方式")

    killed = _stop_daemon()
    if killed:
        print(f"  [OK]   已结束 {killed} 个正在运行的进程")
        removed += 1

    for path in (LAUNCHER, RECORD):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    print("-" * 60)
    if removed:
        print("卸载完成。配置文件、日志与状态已保留在项目目录。")
        print("如需彻底清除，直接删除整个文件夹即可。")
    else:
        print("没有发现需要卸载的注册项。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
