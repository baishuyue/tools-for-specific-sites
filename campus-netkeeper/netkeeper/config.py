"""配置模型与读写。

配置文件是**唯一**的学校相关信息载体：代码里不出现任何具体学校的
域名、IP 段、SSID 或账号。所有字段都有合理默认值，正常情况下用户
只需要填 ``username`` 与 ``password``。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

# 用于探测"是否被强制门户劫持"的地址。这些地址在正常网络下返回
# 204 空响应；一旦设备未认证，接入控制器会把请求劫持并 302 到认证页。
DEFAULT_PROBE_URLS = [
    "http://connect.rom.miui.com/generate_204",
    "http://wifi.vivo.com.cn/generate_204",
    "http://connectivitycheck.platform.hicloud.com/generate_204",
    "http://www.gstatic.com/generate_204",
]

KNOWN_ADAPTERS = ("auto", "eportal", "srun")


@dataclass
class Config:
    # ------------------------------------------------------------ 账号
    username: str = ""
    password: str = ""

    # ------------------------------------------------------------ 门户
    # 认证系统类型：auto 表示自动识别
    adapter: str = "auto"
    # 认证门户地址，例如 https://login.example.edu.cn/eportal
    # 留空则由程序从强制门户的重定向地址中自动发现
    portal_url: str = ""

    # ------------------------------------------------------------ 环境闸门
    # 可选：本机出口 IP 命中任一前缀才认为"在校园网"。
    # 留空表示不做 IP 限制，仅依靠门户可达性判断——这适用于绝大多数场景，
    # 也能避免换校区、换网段后失效。
    campus_ip_prefixes: list[str] = field(default_factory=list)
    # 可选：校园 WiFi 名称。用于自动连接与更严格的环境判定。
    campus_ssids: list[str] = field(default_factory=list)
    # 未连上校园 WiFi 时是否自动切换过去（部分系统需要额外权限）
    auto_connect_wifi: bool = False

    # ------------------------------------------------------------ 节奏
    # tick 模式下由系统调度器决定实际频率，此值仅用于文档与 daemon 模式
    interval: int = 120
    # 保活心跳间隔（秒）。部分学校不校验心跳，设为 0 可完全关闭
    keepalive_interval: int = 300
    http_timeout: float = 8.0
    # 登录失败后的指数退避
    base_backoff: int = 60
    max_backoff: int = 900

    # ------------------------------------------------------------ 功能开关
    # 登录成功后尝试注册 MAC 无感认证（若学校支持）
    enable_mac_auth: bool = True

    # ------------------------------------------------------------ 探测
    probe_urls: list[str] = field(default_factory=lambda: list(DEFAULT_PROBE_URLS))

    # ------------------------------------------------------------ 兜底
    # 无法自动获取认证参数时使用的 queryString（由 capture 命令写入）
    fallback_query: str = ""

    # ------------------------------------------------------------ 适配器微调
    # 少数学校需要特殊参数，例如 {"srun_ac_id": "12", "srun_type": "1"}
    adapter_options: dict[str, str] = field(default_factory=dict)

    # ------------------------------------------------------------ 日志
    log_dir: str = "logs"
    log_level: str = "INFO"
    log_keep_days: int = 14
    state_file: str = "state.json"

    # ------------------------------------------------------------ 校验
    def validate(self) -> list[str]:
        """返回配置问题清单，空列表表示通过。"""
        problems: list[str] = []
        if not self.username.strip():
            problems.append("未填写用户名 username")
        if not self.password:
            problems.append("未填写密码 password")
        if self.adapter not in KNOWN_ADAPTERS:
            problems.append(
                f"adapter={self.adapter!r} 无效，可选：{', '.join(KNOWN_ADAPTERS)}"
            )
        if self.interval < 30:
            problems.append("interval 不应小于 30 秒，过于频繁的检测没有意义")
        if self.keepalive_interval and self.keepalive_interval < 60:
            problems.append("keepalive_interval 不应小于 60 秒（设为 0 表示关闭）")
        if self.max_backoff < self.base_backoff:
            problems.append("max_backoff 不应小于 base_backoff")
        return problems


def _coerce(current: Any, value: Any) -> Any:
    """按字段默认值的类型做轻度类型修正，容忍手写配置的常见错误。"""
    if isinstance(current, bool):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on", "是")
        return bool(value)
    if isinstance(current, int) and not isinstance(value, bool):
        try:
            return int(value)
        except (TypeError, ValueError):
            return current
    if isinstance(current, float):
        try:
            return float(value)
        except (TypeError, ValueError):
            return current
    if isinstance(current, list):
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        if isinstance(value, (list, tuple)):
            return [str(item) for item in value]
        return current
    if isinstance(current, dict):
        if isinstance(value, dict):
            return {str(k): "" if v is None else str(v) for k, v in value.items()}
        return current
    if isinstance(current, str):
        return str(value)
    return value


def load_config(path: Path) -> Config:
    """从 JSON 文件加载配置，缺失项使用默认值。"""
    config = Config()
    if not path.exists():
        return config

    with path.open("r", encoding="utf-8-sig") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict):
        raise ValueError(f"配置文件应为 JSON 对象：{path}")

    known = {f.name for f in fields(Config)}
    for key, value in raw.items():
        if key not in known or key.startswith("_"):
            continue
        setattr(config, key, _coerce(getattr(config, key), value))
    return config


def save_config(path: Path, config: Config) -> None:
    """把配置写回文件（保留未知字段）。"""
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            with path.open("r", encoding="utf-8-sig") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, ValueError):
            existing = {}

    payload = {f.name: getattr(config, f.name) for f in fields(Config)}
    existing.update(payload)
    existing.pop("_说明", None)

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(existing, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    tmp.replace(path)


# 配置模板：注释放在同级的 _说明 里，而不是 JSON 注释，保证可直接被程序读取
CONFIG_TEMPLATE: dict[str, Any] = {
    "_说明": (
        "只需填写 username 与 password 两项即可使用；"
        "portal_url 留空表示自动从强制门户发现。"
        "完整字段说明见 README。"
    ),
    "username": "",
    "password": "",
    "adapter": "auto",
    "portal_url": "",
    "campus_ip_prefixes": [],
    "campus_ssids": [],
    "auto_connect_wifi": False,
    "interval": 120,
    "keepalive_interval": 300,
    "http_timeout": 8.0,
    "base_backoff": 60,
    "max_backoff": 900,
    "enable_mac_auth": True,
    "probe_urls": DEFAULT_PROBE_URLS,
    "fallback_query": "",
    "adapter_options": {},
    "log_dir": "logs",
    "log_level": "INFO",
    "log_keep_days": 14,
    "state_file": "state.json",
}


def write_template(path: Path, *, force: bool = False) -> bool:
    """写出配置模板。已存在且未指定 force 时不覆盖，返回是否写入。"""
    if path.exists() and not force:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(CONFIG_TEMPLATE, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return True
