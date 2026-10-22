"""跨平台网络信息：WiFi 状态、连接控制、默认网关。

只在真正需要时才导入 ``subprocess``，因为 tick 模式下绝大多数轮次
根本用不到 WiFi 控制，省下几 MB 的导入开销。
"""

from __future__ import annotations

import re
import socket
import sys
from typing import Optional


def _run(cmd: list[str], timeout: float = 6.0) -> str:
    """执行命令并返回 stdout（失败返回空串）。"""
    import subprocess  # 延迟导入

    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    for raw in (completed.stdout, completed.stderr):
        if raw:
            for encoding in ("utf-8", "gb18030", "latin-1"):
                try:
                    return raw.decode(encoding)
                except UnicodeDecodeError:
                    continue
    return ""


# ------------------------------------------------------------------ WiFi 状态
def current_ssid() -> Optional[str]:
    """当前关联的无线网络名称；有线连接或读取失败时返回 None。"""
    if sys.platform == "win32":
        return _windows_ssid()
    if sys.platform == "darwin":
        return _macos_ssid()
    return _linux_ssid()


def _windows_ssid() -> Optional[str]:
    text = _run(["netsh", "wlan", "show", "interfaces"])
    if not text:
        return None
    for line in text.splitlines():
        # 中英文系统标签不同，统一按"SSID 后面跟冒号且不是 BSSID"匹配
        stripped = line.strip()
        if not stripped.lower().startswith("ssid"):
            continue
        if "bssid" in stripped.lower():
            continue
        parts = stripped.split(":", 1)
        if len(parts) == 2 and parts[1].strip():
            return parts[1].strip()
    return None


def _macos_ssid() -> Optional[str]:
    text = _run(["networksetup", "-getairportnetwork", "en0"])
    if "Current Wi-Fi Network:" in text:
        return text.split("Current Wi-Fi Network:", 1)[1].strip() or None
    match = re.search(r"Current Wi-Fi Network:\s*(.+)", text)
    return match.group(1).strip() if match else None


def _linux_ssid() -> Optional[str]:
    text = _run(["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"])
    for line in text.splitlines():
        if line.startswith("yes:"):
            return line.split(":", 1)[1].strip() or None
    text = _run(["iwgetid", "-r"])
    return text.strip() or None


def interface_detail() -> dict[str, str]:
    """无线接口的简要信息（用于诊断输出）。"""
    info: dict[str, str] = {}
    if sys.platform == "win32":
        text = _run(["netsh", "wlan", "show", "interfaces"])
        mapping = {
            "名称": "name",
            "name": "name",
            "状态": "state",
            "state": "state",
            "物理地址": "mac",
            "physical address": "mac",
            "信号": "signal",
            "signal": "signal",
        }
        for line in text.splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            key = key.strip().lower()
            for label, field in mapping.items():
                if key == label and field not in info and value.strip():
                    info[field] = value.strip()
                    break
    else:
        info["state"] = current_ssid() or "未连接"
    return info


def connect(ssid: str, timeout: float = 25.0) -> tuple[bool, str]:
    """连接到指定 WiFi（需要系统已保存该网络的配置）。"""
    if sys.platform == "win32":
        output = _run(["netsh", "wlan", "connect", f"name={ssid}"], timeout=timeout)
        if "已成功" in output or "successfully" in output.lower() or not output:
            return True, output.strip() or "已发起连接请求"
        return False, output.strip()

    if sys.platform == "darwin":
        output = _run(["networksetup", "-setairportnetwork", "en0", ssid], timeout=timeout)
        if not output.strip():
            return True, "已发起连接请求"
        return False, output.strip()

    output = _run(["nmcli", "device", "wifi", "connect", ssid], timeout=timeout)
    lowered = output.lower()
    if "successfully activated" in lowered or "已成功激活" in output or not output.strip():
        return True, output.strip() or "已发起连接请求"
    return False, output.strip()


def saved_profiles() -> list[str]:
    """系统里已保存凭据的无线网络列表。"""
    if sys.platform == "win32":
        text = _run(["netsh", "wlan", "show", "profiles"])
        return [
            line.split(":", 1)[1].strip()
            for line in text.splitlines()
            if ":" in line and ("所有用户配置文件" in line or "All User Profile" in line)
        ]
    if sys.platform == "darwin":
        text = _run(["networksetup", "-listpreferredwirelessnetworks", "en0"])
        return [line.strip() for line in text.splitlines()[1:] if line.strip()]
    text = _run(["nmcli", "-t", "-f", "name", "connection", "show"])
    return [line.strip() for line in text.splitlines() if line.strip()]


# ------------------------------------------------------------------ 网关
def dns_suffix() -> Optional[str]:
    """本机所在网络的 DNS 域名后缀。

    学校网络通常会把 DHCP 域设为 ``xxx.edu.cn``，据此可以推测认证门户
    域名（``login.xxx.edu.cn`` 是国内最常见的命名方式）。
    """
    if sys.platform == "win32":
        text = _run(["ipconfig", "/all"])
        for line in text.splitlines():
            if "DNS 后缀" in line or "DNS Suffix" in line:
                if ":" not in line:
                    continue
                value = line.split(":", 1)[1].strip().rstrip(".")
                # 排除"主 DNS 后缀"这类空值与纯数字
                if value and "." in value and not value.replace(".", "").isdigit():
                    return value
        return None

    if sys.platform == "darwin":
        text = _run(["scutil", "--dns"])
        match = re.search(r"search domain\[\d+\]\s*:\s*(\S+)", text)
        return match.group(1).rstrip(".") if match else None

    try:
        with open("/etc/resolv.conf", "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.lower().startswith("search"):
                    parts = line.split()[1:]
                    if parts:
                        return parts[0].rstrip(".")
    except OSError:
        pass
    text = _run(["resolvectl", "domain"])
    for token in text.split():
        if "." in token and token.lower() != "route":
            return token.lstrip("~").rstrip(".")
    return None


def default_gateway() -> Optional[str]:
    """默认网关地址。用于在不知道门户域名时探测本地认证设备。"""
    if sys.platform == "win32":
        text = _run(["ipconfig"])
        for line in text.splitlines():
            lowered = line.lower()
            if ("默认网关" in line or "default gateway" in lowered) and ":" in line:
                candidate = line.split(":", 1)[1].strip()
                if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", candidate):
                    return candidate
        return None

    if sys.platform == "darwin":
        text = _run(["route", "-n", "get", "default"])
        match = re.search(r"gateway:\s*(\d{1,3}(?:\.\d{1,3}){3})", text)
        return match.group(1) if match else None

    text = _run(["ip", "route", "show", "default"])
    match = re.search(r"default via (\d{1,3}(?:\.\d{1,3}){3})", text)
    return match.group(1) if match else None


def local_ip_for(host: str, port: int = 80, timeout: float = 3.0) -> Optional[str]:
    """通过 UDP connect 取到目标主机的本机出口 IP（不实际发包）。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.connect((host, port))
            return sock.getsockname()[0]
    except OSError:
        return None
