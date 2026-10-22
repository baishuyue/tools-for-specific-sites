#!/usr/bin/env python3
"""端到端自检：不依赖任何具体学校，逐项验证本机链路是否可用。

用法::

    python tools/e2e_test.py            # 只读检查 + 在线项验证
    python tools/e2e_test.py --relogin  # 额外做一次「注销 → 重新认证」

``--relogin`` 会造成数秒断网，请勿在开会/下载时使用。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from netkeeper import adapters, netinfo, probe  # noqa: E402
from netkeeper.adapters import SessionStatus  # noqa: E402
from netkeeper.config import load_config  # noqa: E402
from netkeeper.transport import outbound_ip, tls_profile_name  # noqa: E402

PASS, FAIL, SKIP = "通过", "失败", "跳过"
results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "", *, skip: bool = False) -> bool:
    status = SKIP if skip else (PASS if ok else FAIL)
    results.append((name, status, detail))
    mark = {"通过": "[OK]  ", "失败": "[FAIL]", "跳过": "[--]  "}[status]
    print(f"{mark} {name}" + (f" —— {detail}" if detail else ""))
    return ok


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--relogin", action="store_true", help="执行注销→重登验证（会短暂断网）")
    parser.add_argument("--config", default=None)
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    config = load_config(Path(args.config) if args.config else root / "config.json")

    print("=" * 66)
    print("campus-netkeeper 端到端自检")
    print("=" * 66)

    problems = config.validate()
    check("配置完整", not problems, " / ".join(problems))

    ssid = netinfo.current_ssid()
    check("无线已连接", ssid is not None, ssid or "未连接/有线")

    captive = probe.captive(config.probe_urls, timeout=min(config.http_timeout, 6.0))
    check(
        "出口探测",
        captive.state is not probe.CaptiveState.UNREACHABLE,
        f"{captive.state.value} —— {captive.detail}",
    )

    target = None
    if captive.state is probe.CaptiveState.INTERCEPTED:
        base = probe.portal_base_from_url(captive.location)
        if base:
            target = probe.PortalTarget(base_url=base, raw_query=captive.query, source="强制门户")
    if target is None and config.portal_url:
        target = probe.PortalTarget(
            base_url=config.portal_url, raw_query=config.fallback_query, source="配置"
        )
    if target is None:
        check("定位门户", False, "既没被劫持，也没配置 portal_url")
        return _summary()

    name, resolved = adapters.detect(target.base_url, timeout=config.http_timeout)
    if resolved:
        target = probe.PortalTarget(base_url=resolved, raw_query=target.raw_query, source=target.source)
    check("识别认证系统", bool(name), f"{name or '未识别，回退 ePortal'} @ {target.base_url}")

    adapter = adapters.build(
        name or "eportal", target, timeout=config.http_timeout, options=config.adapter_options
    )
    check("TLS 兼容", True, tls_profile_name())

    session = adapter.session()
    check(
        "会话查询",
        session.status is not SessionStatus.UNKNOWN,
        f"{session.status.value} 账号={session.user_id or '-'}",
    )

    if session.status is SessionStatus.ONLINE:
        if adapter.supports_mac_auth:
            ok, detail = adapter.register_mac(session.mac or target.params.get("mac", ""),
                                              session.user_index)
            check("MAC 无感认证", ok, detail)
        else:
            check("MAC 无感认证", True, f"{adapter.label} 不支持，已跳过", skip=True)

        if config.keepalive_interval:
            ok, detail = adapter.keepalive(session.user_index)
            check("会话保活", ok, detail)
    else:
        check("MAC 无感认证", True, "当前不在线，需先登录才能验证", skip=True)
        check("会话保活", True, "当前不在线，需先登录才能验证", skip=True)

    if args.relogin:
        if session.status is not SessionStatus.ONLINE:
            check("注销→重登", True, "当前不在线，跳过", skip=True)
        else:
            print("\n[注意] 即将注销当前会话，约 5 秒后自动重新认证…")
            ok, detail = adapter.logout(session.user_index)
            check("主动注销", ok, detail)
            time.sleep(3)
            outcome = adapter.login(config.username, config.password)
            check("重新认证", outcome.success, outcome.message)
            time.sleep(2)
            again = adapter.session()
            # 注销后若 AC 立刻放行，出口可能本来就是通的；两种结局都算正常
            out = probe.captive(config.probe_urls, timeout=6.0)
            recovered = again.status is SessionStatus.ONLINE or out.state is probe.CaptiveState.OPEN
            check("网络恢复", recovered, f"会话={again.status.value} 出口={out.state.value}")

    return _summary()


def _summary() -> int:
    print("-" * 66)
    failed = [name for name, status, _ in results if status == FAIL]
    print(f"共 {len(results)} 项：通过 {sum(1 for _, s, _ in results if s == PASS)}，"
          f"失败 {len(failed)}，跳过 {sum(1 for _, s, _ in results if s == SKIP)}")
    if failed:
        print("失败项：" + "、".join(failed))
    print("=" * 66)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
