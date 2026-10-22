"""执行器：单轮检测（tick）与常驻循环（daemon）。

为什么默认是 tick 而不是 daemon
-------------------------------
常驻进程即使什么都不做，也要把 Python 解释器、ssl 模块等常驻在内存里，
实测私有内存约 23 MB、工作集约 38 MB。对于一个「每两分钟发几个 HTTP
请求」的任务，这是纯粹的浪费。

``tick`` 模式改为由系统调度器周期性拉起：进程存活 1~3 秒，执行完立即
退出，**空闲时内存占用严格为 0**。所有需要跨轮次保留的信息都落盘到
``state.json``。

对响应速度要求极高（掉线后想秒级恢复）的用户仍可用 ``daemon``。
"""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

from . import adapters, probe
from .adapters import SessionStatus
from .config import Config
from .log import get_logger
from .probe import PortalTarget
from .state import Backoff, State, StateStore
from .transport import outbound_ip, split_url, tcp_open

_log = get_logger()


class TickResult(str, Enum):
    """单轮执行结果。"""

    # ---- 无需介入 ----
    IDLE_NOT_CAMPUS = "idle_not_campus"        # 不在校园网（家里/热点/别的网络）
    IDLE_OTHER_PORTAL = "idle_other_portal"    # 被别的认证门户拦截，不是本校
    IDLE_NO_PORTAL = "idle_no_portal"          # 不知道门户地址，也无从发现
    IDLE_OFFLINE = "idle_offline"              # 出口完全不通（断网/未获 IP）
    OPEN_NO_SESSION = "open_no_session"        # 出口通畅但无会话记录（MAC 无感放行等）
    SESSION_UNKNOWN = "session_unknown"        # 会话状态查询失败，本轮不介入
    BACKOFF = "backoff"                        # 退避等待中

    # ---- 已介入 ----
    ONLINE = "online"                          # 已认证在线
    LOGGED_IN = "logged_in"                    # 本轮完成登录
    OCCUPIED = "occupied"                      # 出口 IP 被其他账号占用
    AUTH_FAILED = "auth_failed"                # 认证被拒绝
    NO_QUERY = "no_query"                      # 取不到认证参数
    ERROR = "error"                            # 未预期异常

    @property
    def is_actionable(self) -> bool:
        return self in (TickResult.ONLINE, TickResult.LOGGED_IN)

    @property
    def is_failure(self) -> bool:
        return self in (
            TickResult.AUTH_FAILED,
            TickResult.NO_QUERY,
            TickResult.ERROR,
        )


@dataclass
class Runner:
    config: Config
    root: Path
    state: State = None  # type: ignore[assignment]
    store: StateStore = None  # type: ignore[assignment]
    detail: str = ""
    adapter_name: str = ""
    portal_url: str = ""

    def __post_init__(self) -> None:
        self.store = StateStore(self.root / self.config.state_file)
        self.state = self.store.load()
        self._backoff = Backoff(
            self.store, self.state, self.config.base_backoff, self.config.max_backoff
        )

    # ================================================================ 辅助
    def _save(self) -> None:
        self.store.save(self.state)

    def _campus_gate(self, host: str) -> bool:
        """可选闸门：出口 IP 必须命中配置的校园网段。

        默认不启用（``campus_ip_prefixes`` 为空），因为换校区、换网段后
        这道闸门会让工具静默失效；依靠"门户可达 + 被本校门户拦截"这两条
        信号已经足够准确。
        """
        prefixes = [p.strip() for p in self.config.campus_ip_prefixes if p.strip()]
        if not prefixes or not host:
            return True
        ip = outbound_ip(host)
        if not ip:
            return True  # 取不到就放行，交给后续判定
        return any(ip.startswith(prefix) for prefix in prefixes)

    def _ensure_wifi(self) -> bool:
        """按需切换到校园 WiFi。返回 True 表示可以继续。"""
        cfg = self.config
        if not cfg.auto_connect_wifi or not cfg.campus_ssids:
            return True

        from . import netinfo

        current = netinfo.current_ssid()
        if current in cfg.campus_ssids:
            return True
        if current is None:
            # 有线环境下 SSID 为空，不强行拉起无线
            return True

        target = cfg.campus_ssids[0]
        _log.info("当前无线为「%s」，尝试切换到「%s」", current, target)
        ok, detail = netinfo.connect(target)
        if ok:
            _log.info("无线切换成功：%s", detail)
        else:
            _log.warning("无线切换失败：%s", detail)
        return ok

    # ================================================================ 主流程
    def tick(self) -> TickResult:
        cfg = self.config
        self.state.last_tick_at = time.time()

        # ---------- 1. 出口探测 ----------
        captive = probe.captive(cfg.probe_urls, timeout=min(cfg.http_timeout, 6.0))

        if captive.state is probe.CaptiveState.UNREACHABLE:
            self.detail = captive.detail
            self._save()
            return TickResult.IDLE_OFFLINE

        target: Optional[PortalTarget] = None

        # ---------- 2. 被劫持：优先采用劫持页里的门户地址 ----------
        if captive.state is probe.CaptiveState.INTERCEPTED:
            discovered = probe.portal_base_from_url(captive.location)
            if not discovered:
                self.detail = f"收到重定向但无法解析门户地址：{captive.location[:80]}"
                self._save()
                return TickResult.IDLE_NO_PORTAL

            configured = (cfg.portal_url or "").strip()
            if configured and not probe.same_host(configured, discovered):
                # 拦截我们的是另一个门户。若本校门户仍可达，说明只是被
                # 跨校/公共网络的页面挡住；否则直接判定为"不在本校网络"。
                if not tcp_open(split_url(configured)[1], 443, timeout=3.0):
                    self.detail = f"当前网络的门户（{discovered}）不是配置的门户"
                    self._save()
                    return TickResult.IDLE_OTHER_PORTAL

            target = PortalTarget(
                base_url=discovered,
                raw_query=captive.query,
                source="强制门户劫持",
            )

        # ---------- 3. 环境闸门 ----------
        gate_host = (target.host if target else "") or split_url(cfg.portal_url)[1]
        if not self._campus_gate(gate_host):
            self.detail = f"出口 IP 不属于配置的校园网段（{', '.join(cfg.campus_ip_prefixes)}）"
            self._save()
            return TickResult.IDLE_NOT_CAMPUS

        if not self._ensure_wifi():
            self._save()
            return TickResult.IDLE_OFFLINE

        # ---------- 4. 确定门户地址 ----------
        if target is None:
            cached = (self.state.portal_url or "").strip()
            configured = (cfg.portal_url or "").strip()
            known = cached or configured
            if not known:
                # 没配置也没缓存：从默认网关或门户域名尝试发现
                from . import netinfo

                target = probe.from_gateway(netinfo.default_gateway() or "")
                if target is None:
                    self.detail = "未配置 portal_url，且无法自动发现门户地址"
                    self._save()
                    return TickResult.IDLE_NO_PORTAL
            else:
                target = PortalTarget(base_url=known, raw_query="", source="配置/缓存")

        # ---------- 5. 构造适配器 ----------
        adapter = adapters.resolve_and_build(
            cfg.adapter,
            target,
            timeout=cfg.http_timeout,
            options=cfg.adapter_options,
        )
        self.adapter_name = adapter.name
        self.portal_url = adapter.target.base_url
        if self.state.portal_url != adapter.target.base_url:
            self.state.portal_url = adapter.target.base_url

        # ---------- 6. 查询会话 ----------
        session = adapter.session()

        if session.status is SessionStatus.UNKNOWN:
            # 查询失败既不能断言在线也不能断言离线，保持原状等下一轮，
            # 避免因一次抖动就重复提交认证
            self.detail = session.detail
            self._save()
            return TickResult.SESSION_UNKNOWN

        if session.status is SessionStatus.ONLINE:
            if session.user_id and cfg.username and session.user_id != cfg.username:
                self.detail = f"当前出口 IP 上的在线账号是 {session.user_id}"
                self._save()
                return TickResult.OCCUPIED

            self.state.user_index = session.user_index or self.state.user_index
            self.state.last_online_at = time.time()
            self.state.adapter = adapter.name
            self._maybe_keepalive(adapter, session)
            self._maybe_register_mac(adapter, session)
            self._backoff.reset()
            self._save()
            self.detail = f"在线账号 {session.user_id or cfg.username}"
            return TickResult.ONLINE

        # ---------- 7. 离线判定 ----------
        if captive.state is probe.CaptiveState.OPEN:
            # 能直接上网但没有会话记录：可能是免认证网段，或 MAC 无感放行。
            # 此时绝不做任何写操作，避免打断正在正常工作的网络。
            self.state.last_online_at = time.time()
            self._save()
            self.detail = "出口通畅但未查到认证会话，不介入"
            return TickResult.OPEN_NO_SESSION

        # ---------- 8. 确实需要认证 ----------
        if self._backoff.blocked():
            self.detail = f"退避中，还需 {int(self._backoff.remaining())} 秒"
            self._save()
            return TickResult.BACKOFF

        return self._login(adapter, captive)

    # ---------------------------------------------------------------- 登录
    def _login(self, adapter, captive: probe.CaptiveResult) -> TickResult:
        cfg = self.config

        # 取认证参数：劫持地址 > 门户首页重定向 > 本地兜底
        raw_query = adapter.target.raw_query
        source = adapter.target.source
        if not raw_query or adapter.needs_query:
            if not raw_query:
                from .probe import probe_portal_root

                found = probe_portal_root(adapter.target.base_url, timeout=cfg.http_timeout)
                if found and found.raw_query:
                    raw_query, source = found.raw_query, found.source
            if not raw_query and cfg.fallback_query:
                raw_query, source = cfg.fallback_query, "本地兜底参数"

        if adapter.needs_query and not raw_query:
            reason = (
                "取不到认证参数（wlanuserip/mac/nasid 之类）。"
                "请用浏览器打开一次认证页，然后运行 `netkeeper capture` 保存参数"
            )
            self._backoff.fail(reason)
            self.detail = reason
            return TickResult.NO_QUERY

        adapter.target.raw_query = raw_query

        # 复用上次探测成功的编码方式，避免重复试错
        variant = self.state.encode_variant if self.state.adapter == adapter.name else ""

        _log.info(
            "检测到需要认证（来源：%s），开始登录 %s", source or "未知", cfg.username
        )
        outcome = adapter.login(cfg.username, cfg.password, variant=variant)
        self.state.login_attempts += 1

        if not outcome.success:
            reason = outcome.hint or outcome.message
            self._backoff.fail(f"登录被拒绝：{reason}")
            self.detail = reason
            return TickResult.AUTH_FAILED

        now = time.time()
        self.state.username = cfg.username
        self.state.adapter = adapter.name
        self.state.user_index = outcome.user_index or self.state.user_index
        self.state.encode_variant = outcome.variant or self.state.encode_variant
        self.state.last_login_at = now
        self.state.last_online_at = now
        self.state.last_keepalive_at = now  # 刚登录，不必立刻保活
        self.state.mac_registered = False
        self._backoff.reset()
        self._save()
        _log.info(
            "[成功] 认证通过（适配器=%s，编码=%s，userIndex=%s）",
            adapter.name,
            outcome.variant or "-",
            self.state.user_index or "未返回",
        )

        self._register_mac_after_login(adapter)
        self._verify(adapter)
        self.detail = outcome.message or "认证成功"
        return TickResult.LOGGED_IN

    def _verify(self, adapter) -> None:
        """登录后复核一次，确认会话真的建立起来了。"""
        time.sleep(1.5)
        session = adapter.session()
        if session.status is SessionStatus.ONLINE:
            self.state.user_index = session.user_index or self.state.user_index
            self._save()
            _log.info("[成功] 会话已确认建立")
        elif session.status is SessionStatus.UNKNOWN:
            _log.info("[提示] 登录后复核暂未完成（%s），不影响已建立的会话", session.detail)
        else:
            _log.warning("[警告] 登录接口返回成功但会话复核未通过，下一轮会重试")

    def _maybe_keepalive(self, adapter, session) -> None:
        interval = self.config.keepalive_interval
        if not interval or not self.state.user_index:
            return
        if time.time() - self.state.last_keepalive_at < interval:
            return

        ok, detail = adapter.keepalive(self.state.user_index)
        self.state.last_keepalive_at = time.time()
        if ok:
            _log.debug("[保活] 成功（%s）", detail)
        else:
            _log.warning("[保活] 失败（%s），下一轮将重新认证", detail)
            self.state.user_index = ""

    def _maybe_register_mac(self, adapter, session) -> None:
        """在线状态下补注册 MAC 无感认证（仅当尚未注册时）。"""
        cfg = self.config
        if not cfg.enable_mac_auth or not adapter.supports_mac_auth:
            return
        if self.state.mac_registered:
            return
        mac = self._current_mac(adapter, session)
        if not mac:
            return
        self._do_register_mac(adapter, mac)

    def _register_mac_after_login(self, adapter) -> None:
        cfg = self.config
        if not cfg.enable_mac_auth or not adapter.supports_mac_auth:
            return
        mac = self._current_mac(adapter, None)
        if mac:
            self._do_register_mac(adapter, mac)

    def _current_mac(self, adapter, session) -> str:
        if session is not None and session.mac:
            return session.mac
        params = adapter.target.params
        return params.get("mac") or params.get("wlanmac") or ""

    def _do_register_mac(self, adapter, mac: str) -> None:
        ok, detail = adapter.register_mac(mac, self.state.user_index)
        if ok:
            self.state.mac_registered = True
            self.state.mac_registered_value = mac
            _log.info("[无感] %s —— 此后接入校园网将由 AC 自动放行", detail)
        else:
            _log.info("[无感] MAC 无感认证未启用（%s），由自动重登兜底", detail)
        self._save()

    # ================================================================ 循环
    def run_forever(self) -> None:
        cfg = self.config
        _log.info(
            "守护进程启动：账号=%s 门户=%s 间隔=%ds 保活=%ds",
            cfg.username,
            cfg.portal_url or "(自动发现)",
            cfg.interval,
            cfg.keepalive_interval,
        )
        while True:
            try:
                result = self.tick()
                _log.debug("本轮：%s（%s）", result.value, self.detail)
            except KeyboardInterrupt:
                _log.info("收到中断信号，退出")
                return
            except Exception as exc:  # noqa: BLE001 - 单轮异常不得终止守护
                _log.exception("本轮异常：%s", exc)
                result = TickResult.ERROR

            if result is TickResult.BACKOFF:
                sleep_for = max(5.0, min(self._backoff.remaining(), cfg.max_backoff))
            elif result.is_failure:
                sleep_for = max(10.0, min(self._backoff.remaining() or cfg.interval, cfg.max_backoff))
            else:
                sleep_for = cfg.interval

            try:
                time.sleep(sleep_for)
            except KeyboardInterrupt:
                _log.info("收到中断信号，退出")
                return


# ------------------------------------------------------------------- 单实例
class SingleInstance:
    """用本地端口占用实现跨平台单实例锁（仅 daemon 模式需要）。"""

    def __init__(self, port: int = 52818) -> None:
        self.port = port
        self._sock: Optional[socket.socket] = None

    def acquire(self) -> bool:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # 刻意不设 SO_REUSEADDR：否则 Windows 上允许重复绑定，锁就失效了
        try:
            sock.bind(("127.0.0.1", self.port))
        except OSError:
            sock.close()
            return False
        self._sock = sock
        return True

    def release(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None
