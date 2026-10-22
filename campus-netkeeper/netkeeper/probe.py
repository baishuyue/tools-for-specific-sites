"""网络环境探测：判断出口是否被强制门户劫持，并从中还原门户地址与认证参数。

这里是"通用性"的关键所在
------------------------
传统做法是把学校门户域名写进配置，换一所学校就得改代码。本模块改从
**强制门户的 302 重定向**里反推门户地址与认证参数（wlanuserip / mac /
nasid ...），因此对任意一所使用 Web Portal 认证的学校都成立——只要
未认证时 AC 会把 HTTP 请求劫持到认证页，而这正是 Portal 认证的定义。
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .transport import request, split_url, tcp_open


class CaptiveState(str, Enum):
    OPEN = "open"                # 直连成功：已认证，或根本不在受限网络
    INTERCEPTED = "intercepted"  # 被强制门户劫持，需要认证
    UNREACHABLE = "unreachable"  # 探测目标完全不可达（断网 / DNS 故障）


@dataclass
class CaptiveResult:
    state: CaptiveState
    location: str = ""
    detail: str = ""
    probe_url: str = ""

    @property
    def query(self) -> str:
        """重定向地址里的原始查询串。"""
        return query_of(self.location)


@dataclass
class PortalTarget:
    """一个可用的认证门户。"""

    base_url: str          # 例如 https://login.example.edu.cn/eportal
    raw_query: str = ""    # 认证参数
    source: str = ""       # 来源说明

    @property
    def host(self) -> str:
        return split_url(self.base_url)[1]

    @property
    def scheme(self) -> str:
        return split_url(self.base_url)[0]

    @property
    def params(self) -> dict[str, str]:
        return dict(urllib.parse.parse_qsl(self.raw_query, keep_blank_values=True))


# ------------------------------------------------------------------ 工具
def query_of(url: str) -> str:
    if not url:
        return ""
    return urllib.parse.urlsplit(url).query


def portal_base_from_url(url: str) -> str:
    """从一个具体的认证页地址还原门户根地址。

    ``https://h/eportal/index.jsp?x=1`` -> ``https://h/eportal``
    ``https://h/eportal/``             -> ``https://h/eportal``
    ``http://h/``                      -> ``https://h``（若 443 可用）
    """
    scheme, host, path = split_url(url)
    if not host:
        return ""
    # 去掉常见的页面文件名后缀，保留上级目录作为门户根
    for tail in ("/index.jsp", "/index.html", "/index.htm", "/portal.jsp", "/login.jsp"):
        if path.lower().endswith(tail):
            path = path[: -len(tail)]
            break
    base = f"{scheme}://{host}{path}".rstrip("/")
    if scheme == "http" and tcp_open(host, 443, timeout=2.0):
        base = "https://" + base[len("http://") :]
    return base


def same_host(url_a: str, url_b: str) -> bool:
    host_a = split_url(url_a)[1].lower()
    host_b = split_url(url_b)[1].lower()
    return bool(host_a) and host_a == host_b


def looks_like_portal(location: str) -> bool:
    """粗判重定向目标是不是一个 Web 认证门户。"""
    if not location:
        return False
    lowered = location.lower()
    hints = ("eportal", "portal", "wlanuserip", "srun_portal", "get_challenge", "ac_id=")
    return any(hint in lowered for hint in hints)


# ------------------------------------------------------------------ 探测
def captive(probe_urls: list[str], timeout: float = 6.0) -> CaptiveResult:
    """探测出口是否被强制门户劫持。"""
    last_detail = ""
    for url in probe_urls:
        try:
            resp = request(url, timeout=timeout, follow_redirects=False)
        except Exception as exc:  # noqa: BLE001 - 探测失败不应中断流程
            last_detail = f"{url} -> {type(exc).__name__}"
            continue

        if resp.status in (301, 302, 303, 307, 308):
            location = resp.location
            if looks_like_portal(location):
                return CaptiveResult(
                    state=CaptiveState.INTERCEPTED,
                    location=location,
                    detail="被认证门户劫持",
                    probe_url=url,
                )
            last_detail = f"{url} -> {resp.status}"
            continue

        if resp.status == 204:
            return CaptiveResult(
                state=CaptiveState.OPEN, detail="直连成功（204）", probe_url=url
            )

        if 200 <= resp.status < 400:
            # 有的网络把探测地址重写为 200 空响应或欢迎页，同样视为通
            return CaptiveResult(
                state=CaptiveState.OPEN, detail=f"直连成功（{resp.status}）", probe_url=url
            )

        last_detail = f"{url} -> HTTP {resp.status}"

    return CaptiveResult(
        state=CaptiveState.UNREACHABLE, detail=last_detail or "所有探测地址均不可达"
    )


def from_gateway(gateway: str, timeout: float = 4.0) -> Optional[PortalTarget]:
    """访问默认网关，看它是否把请求推送到认证页。

    有些学校的 AC 只对特定域名做劫持，``generate_204`` 域名可能被放行，
    此时从网关根路径往往能直接拿到门户地址。
    """
    if not gateway:
        return None
    try:
        resp = request(f"http://{gateway}/", timeout=timeout, follow_redirects=False)
    except Exception:  # noqa: BLE001
        return None
    if resp.status in (301, 302, 303, 307, 308) and looks_like_portal(resp.location):
        base = portal_base_from_url(resp.location)
        if base:
            return PortalTarget(base_url=base, raw_query=query_of(resp.location), source="默认网关")
    # 网关自身就是门户页
    if resp.status == 200 and "eportal" in resp.text.lower():
        base = portal_base_from_url(f"http://{gateway}/eportal")
        return PortalTarget(base_url=base, raw_query="", source="默认网关")
    return None


def probe_portal_root(base_url: str, timeout: float = 4.0) -> Optional[PortalTarget]:
    """访问门户根路径，从 302 中取认证参数。

    已认证时会跳转到成功页（无参数）；未认证时跳回 ``index.jsp?wlanuserip=...``。
    这是除强制门户劫持之外的第二条取参路径。
    """
    for candidate in (base_url, base_url.replace("https://", "http://", 1)):
        host_root = ""
        scheme, host, _ = split_url(candidate)
        if host:
            host_root = f"{scheme}://{host}"
        for url in (candidate + "/", host_root + "/"):
            if not url or url == "//":
                continue
            try:
                resp = request(url, timeout=timeout, follow_redirects=False)
            except Exception:  # noqa: BLE001
                continue
            if resp.status in (301, 302, 303, 307, 308):
                query = query_of(resp.location)
                if "wlanuserip" in query or "mac=" in query or "ac_id=" in query:
                    base = portal_base_from_url(resp.location) or base_url
                    return PortalTarget(base_url=base, raw_query=query, source="门户首页重定向")
            elif resp.status == 200:
                return PortalTarget(base_url=base_url, raw_query="", source="门户首页")
    return None


# 国内高校认证门户最常见的几种命名，配合 DNS 后缀做启发式发现
_PORTAL_HOST_PREFIXES = ("login", "portal", "eportal", "auth", "wlan", "net", "wifi", "sso")


def by_dns_suffix(suffix: str, timeout: float = 3.0) -> Optional[PortalTarget]:
    """按 ``<前缀>.<学校域名>`` 猜测认证门户。

    这是"没被劫持、也没配置地址"时最后的自动手段。命中率不算高，但
    试探成本极低且完全通用：只对后缀下真实存在的候选逐一做一次页面探测。
    """
    if not suffix or "." not in suffix:
        return None
    for prefix in _PORTAL_HOST_PREFIXES:
        host = f"{prefix}.{suffix}"
        for scheme in ("https", "http"):
            base = f"{scheme}://{host}"
            try:
                resp = request(base + "/index.jsp", timeout=timeout, follow_redirects=False)
            except Exception:  # noqa: BLE001
                break  # 该主机不通，换下一个前缀
            if resp.status == 200 and "interface.do" in resp.text.lower():
                return PortalTarget(base_url=f"{base}/eportal", raw_query="", source="DNS 后缀猜测")
            if resp.status in (301, 302, 303, 307, 308):
                base2 = portal_base_from_url(resp.location)
                if base2:
                    return PortalTarget(base_url=base2, raw_query=query_of(resp.location), source="DNS 后缀猜测")
            break
    return None
