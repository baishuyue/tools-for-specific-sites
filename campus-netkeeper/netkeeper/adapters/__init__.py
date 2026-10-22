"""适配器注册表与自动识别。"""

from __future__ import annotations

from typing import Optional

from ..probe import PortalTarget
from ..transport import split_url
from .base import Adapter, LoginOutcome, SessionInfo, SessionStatus, explain
from .eportal import EportalAdapter
from .srun import SrunAdapter

__all__ = [
    "Adapter",
    "LoginOutcome",
    "SessionInfo",
    "SessionStatus",
    "explain",
    "ADAPTERS",
    "available",
    "build",
    "detect",
]

ADAPTERS: dict[str, type[Adapter]] = {
    EportalAdapter.name: EportalAdapter,
    SrunAdapter.name: SrunAdapter,
}


def available() -> list[str]:
    return list(ADAPTERS)


def _eportal_base(base_url: str, timeout: float) -> Optional[str]:
    """找出 ePortal 真正可用的接口根路径（可能带 /eportal 后缀）。"""
    return EportalAdapter.resolve_base(base_url, timeout)


def detect(base_url: str, timeout: float = 4.0) -> tuple[str, str]:
    """自动识别门户类型，返回 ``(适配器名, 规范化后的门户地址)``。

    识别顺序有讲究：先用页面特征判断 ePortal（成本低、特征明确），
    再用 challenge 接口判断深澜。
    """
    if not base_url:
        return "", ""

    resolved = _eportal_base(base_url, timeout)
    if resolved:
        return EportalAdapter.name, resolved

    if SrunAdapter.detect(base_url, timeout):
        return SrunAdapter.name, base_url.rstrip("/")

    # 都识别不出来：退回 ePortal，它是国内覆盖率最高的一套
    return "", ""


def build(
    name: str,
    target: PortalTarget,
    *,
    timeout: float = 8.0,
    options: Optional[dict[str, str]] = None,
) -> Adapter:
    """按名称构造适配器实例。"""
    cls = ADAPTERS.get(name)
    if cls is None:
        cls = EportalAdapter
    return cls(target, timeout=timeout, options=options)


def resolve_and_build(
    preferred: str,
    target: PortalTarget,
    *,
    timeout: float = 8.0,
    options: Optional[dict[str, str]] = None,
) -> Adapter:
    """按配置的 ``adapter`` 构造实例；``auto`` 时先识别再构造。"""
    if preferred and preferred != "auto":
        return build(preferred, target, timeout=timeout, options=options)

    name, resolved = detect(target.base_url, timeout=min(timeout, 5.0))
    if resolved:
        target = PortalTarget(
            base_url=resolved, raw_query=target.raw_query, source=target.source
        )
    return build(name or EportalAdapter.name, target, timeout=timeout, options=options)


def portal_host_of(base_url: str) -> str:
    return split_url(base_url)[1]
