"""适配器公共接口。

新增一所学校/一套认证系统时，只需实现一个 :class:`Adapter` 子类并在
``adapters/__init__.py`` 里注册，不需要改动主流程。这是本项目"通用性"
的结构保证。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from ..probe import PortalTarget


class SessionStatus(str, Enum):
    """会话查询结果。

    必须区分"查询失败"与"确实离线"：把网络抖动误判为离线，客户端会
    不停重复提交认证，既无意义又可能触发风控锁定。
    """

    ONLINE = "online"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


@dataclass
class SessionInfo:
    status: SessionStatus
    user_id: str = ""
    user_index: str = ""
    mac: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    detail: str = ""


@dataclass
class LoginOutcome:
    success: bool
    message: str = ""
    user_index: str = ""
    hint: str = ""
    variant: str = ""
    raw: Optional[dict[str, Any]] = None


# 服务端原始提示 -> 面向用户的可操作建议。
# 各校文案差异很大，因此按关键词模糊匹配。
_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("密码错误", "用户名或密码", "password error", "invalid password"),
     "账号或密码不正确，请核对配置"),
    (("用户不存在", "账号不存在", "no such user", "user_not_found"),
     "账号不存在，请确认学号/工号"),
    (("已达到", "达到", "超出", "上限", "device limit", "too many"),
     "已达该账号的在线设备数上限，需要先在其他设备上下线；"
     "本工具无法也不应绕过这一限制"),
    (("欠费", "余额不足", "arrear", "insufficient"),
     "账号欠费或套餐到期，请到校园网自助服务系统处理"),
    (("暂停", "禁用", "冻结", "suspend", "frozen"),
     "账号已被停用，请联系网络中心"),
    (("绑定", "bind"), "账号与设备的绑定关系异常，请联系网络中心"),
    (("验证码", "captcha", "validcode"), "该门户要求输入验证码，无法自动登录"),
    (("认证被拒绝", "reject"), "认证被拒绝，可能是账号状态或设备限制问题"),
)


def explain(message: str) -> str:
    """把服务端提示翻译成可操作建议；无匹配时原样返回。"""
    text = (message or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    for keywords, hint in _HINTS:
        if any(k in text or k in lowered for k in keywords):
            return hint
    return text


def is_credential_error(message: str) -> bool:
    """判断失败是否可能由"参数编码方式不同"引起。

    需要用这个信号决定是否换一种编码再试一次：不同学校的 ePortal 对
    用户名/密码的编码层数不一致，编码错误时服务端通常报"用户名或密码错误"。
    """
    text = (message or "").lower()
    signals = ("密码", "password", "用户名", "user", "参数", "param", "非法", "invalid")
    return any(k in text for k in signals)


class Adapter:
    """认证系统适配器基类。"""

    #: 适配器标识，与配置文件里的 adapter 字段对应
    name: str = "base"
    #: 人类可读名称
    label: str = "通用"
    #: 是否需要认证参数（queryString）
    needs_query: bool = True
    #: 是否支持 MAC 无感认证注册
    supports_mac_auth: bool = False

    def __init__(
        self,
        target: PortalTarget,
        *,
        timeout: float = 8.0,
        options: Optional[dict[str, str]] = None,
    ) -> None:
        self.target = target
        self.timeout = timeout
        self.options = options or {}

    # ------------------------------------------------------------ 识别
    @classmethod
    def detect(cls, base_url: str, timeout: float = 4.0) -> bool:
        """探测给定门户是否由本适配器支持。"""
        return False

    # ------------------------------------------------------------ 动作
    def session(self) -> SessionInfo:
        """查询本机出口 IP 上的会话状态。"""
        return SessionInfo(status=SessionStatus.UNKNOWN, detail="未实现")

    def login(self, username: str, password: str, *, variant: str = "") -> LoginOutcome:
        """提交认证。"""
        return LoginOutcome(success=False, message="未实现", hint="该适配器不支持登录")

    def keepalive(self, user_index: str) -> tuple[bool, str]:
        """保活心跳。返回 (是否仍然在线, 说明)。"""
        return True, "无需保活"

    def register_mac(self, mac: str, user_index: str) -> tuple[bool, str]:
        """注册 MAC 无感认证。"""
        return False, "该校认证系统不支持 MAC 无感认证"

    def logout(self, user_index: str) -> tuple[bool, str]:
        """主动下线。"""
        return False, "该校认证系统不支持主动下线"

    # ------------------------------------------------------------ 调试
    def describe(self) -> dict[str, str]:
        """诊断信息，供 ``netkeeper doctor`` 输出。"""
        return {
            "适配器": f"{self.label} ({self.name})",
            "门户地址": self.target.base_url,
            "参数来源": self.target.source or "-",
            "参数字段": ", ".join(self.target.params.keys()) or "-",
        }
