"""命令行入口。

子命令一览::

    netkeeper              默认执行一次 tick（推荐，供系统调度器调用）
    netkeeper tick         同上
    netkeeper daemon       常驻循环（占内存，仅在需要秒级响应时使用）
    netkeeper init         交互式初始化：填账号密码、自动发现门户
    netkeeper status       查看配置与运行状态
    netkeeper doctor       诊断：探测门户、识别认证系统、逐项自检
    netkeeper login        立即认证一次
    netkeeper logout       主动下线
    netkeeper mac-register 注册 MAC 无感认证
    netkeeper capture      保存认证参数（兜底）
    netkeeper wifi         查看无线状态
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import urllib.parse
from pathlib import Path
from typing import Optional

from . import __version__, adapters, netinfo, probe
from .adapters import SessionStatus
from .config import Config, load_config, save_config, write_template
from .log import setup_logging
from .runner import Runner, SingleInstance, TickResult
from .state import StateStore
from .transport import split_url, tls_profile_name


# ------------------------------------------------------------------ 上下文
def _config_path(arg: Optional[str], *, for_write: bool = False) -> Path:
    """定位配置文件。源码运行时放在仓库根目录，安装后放在当前目录。"""
    if arg:
        return Path(arg).expanduser().resolve()
    env = os.environ.get("NETKEEPER_CONFIG")
    if env:
        return Path(env).expanduser().resolve()

    package_parent = Path(__file__).resolve().parent.parent
    if (package_parent / "pyproject.toml").exists():
        candidate = package_parent / "config.json"
    else:
        candidate = Path.cwd() / "config.json"

    if candidate.exists() or for_write:
        return candidate
    # 已安装但当前目录没有配置时，回落到用户配置目录
    fallback = Path.home() / ".config" / "netkeeper" / "config.json"
    return candidate if candidate.parent.exists() else fallback


def _load(args) -> tuple[Config, Path, Path]:
    path = _config_path(getattr(args, "config", None))
    if not path.exists():
        write_template(path)
        print(f"已生成配置模板：{path}")
        print("请填入学号与密码，或运行 `netkeeper init` 交互式配置。")
        raise SystemExit(2)
    config = load_config(path)
    return config, path, path.parent


def _prepare(config: Config, root: Path, *, level: Optional[str] = None, console: bool = False):
    setup_logging(
        root / config.log_dir,
        level=level or config.log_level,
        keep_days=config.log_keep_days,
        console=console,
    )


def _mask(secret: str) -> str:
    if not secret:
        return "(空)"
    if len(secret) <= 2:
        return "*" * len(secret)
    return secret[0] + "*" * (len(secret) - 2) + secret[-1]


def _ts(value: float) -> str:
    import time

    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value)) if value else "-"


def _stdin_interactive() -> bool:
    """判断当前是否真的能交互输入。

    某些终端模拟器会把重定向后的 stdin 也报告为 tty，因此这里额外
    依赖调用方捕获 EOFError 兜底。
    """
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _hr(title: str = "") -> None:
    print("=" * 64)
    if title:
        print(title)
        print("=" * 64)


# ------------------------------------------------------------------ 子命令
def cmd_init(args) -> int:
    """交互式初始化向导。"""
    path = _config_path(getattr(args, "config", None), for_write=True)
    config = load_config(path) if path.exists() else Config()

    print("校园网自动认证 · 初始化向导")
    print("-" * 64)
    print(f"配置文件：{path}")
    print()

    if _stdin_interactive():
        current = f"（当前：{config.username}）" if config.username else ""
        try:
            entered = input(f"学号 / 账号{current}: ").strip()
            if entered:
                config.username = entered

            if config.password:
                changed = input("密码（留空保持不变）: ").strip()
                if changed:
                    config.password = changed
            else:
                config.password = getpass.getpass("密码: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[提示] 输入已中断，保留现有配置")
    else:
        print("[提示] 非交互环境，保留现有账号配置")

    if not config.username or not config.password:
        print("\n[错误] 账号或密码为空，已中止。")
        return 2

    print("\n正在探测网络环境…")
    target = _discover_portal(config, interactive=_stdin_interactive())
    if target:
        config.portal_url = target.base_url
        print(f"[成功] 已发现认证门户：{target.base_url}")
        name, resolved = adapters.detect(target.base_url, timeout=config.http_timeout)
        if resolved:
            config.portal_url = resolved
        if name:
            config.adapter = name
            print(f"[成功] 识别到认证系统：{adapters.ADAPTERS[name].label}")
        else:
            config.adapter = "auto"
            print("[提示] 未能确定认证系统类型，将使用自动识别")
        if target.raw_query:
            config.fallback_query = target.raw_query
    else:
        print("[提示] 当前无法自动发现门户地址（可能不在校园网内）。")
        print("       可以稍后在校园网内重新运行 `netkeeper init`，")
        print("       或手动填写配置里的 portal_url。")

    save_config(path, config)
    print(f"\n[完成] 配置已写入 {path}")
    print("下一步：运行 `netkeeper tick` 验证能否认证；")
    print("        验证通过后运行部署脚本安装开机自启。")
    return 0


def _discover_portal(config: Config, *, interactive: bool) -> Optional[probe.PortalTarget]:
    """按可靠性顺序尝试发现门户地址。

    1. 强制门户劫持 —— 未认证时必然发生，最可靠
    2. 已配置 / 已缓存的门户地址
    3. 默认网关推送
    4. DNS 后缀启发式（login.<学校域名> 等常见命名）
    5. 用户手动粘贴
    """
    captive = probe.captive(config.probe_urls, timeout=min(config.http_timeout, 6.0))
    if captive.state is probe.CaptiveState.INTERCEPTED:
        base = probe.portal_base_from_url(captive.location)
        if base:
            return probe.PortalTarget(base_url=base, raw_query=captive.query, source="强制门户劫持")

    configured = (config.portal_url or "").strip()
    if configured:
        found = probe.probe_portal_root(configured, timeout=config.http_timeout)
        if found:
            return found

    gateway = netinfo.default_gateway()
    if gateway:
        found = probe.from_gateway(gateway, timeout=config.http_timeout)
        if found:
            return found

    suffix = netinfo.dns_suffix()
    if suffix:
        print(f"（尝试按 DNS 后缀 {suffix} 猜测门户地址…）")
        found = probe.by_dns_suffix(suffix, timeout=3.0)
        if found:
            return found

    if not interactive:
        return None

    print()
    print("没能自动发现认证门户。原因是：自动发现依赖「未认证时被强制跳转」，")
    print("而当前这台机器已经在上网（或者根本不在校园网内）。")
    print("请用浏览器打开一次校园网登录页，把地址栏内容完整粘贴到下面。")
    try:
        typed = input("认证页地址（留空跳过）: ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not typed:
        return None
    base = probe.portal_base_from_url(typed)
    if not base:
        print("[警告] 无法从该地址解析出门户，已跳过。")
        return None
    return probe.PortalTarget(base_url=base, raw_query=probe.query_of(typed), source="手动输入")


def cmd_tick(args) -> int:
    config, path, root = _load(args)
    _prepare(config, root, level="WARNING" if getattr(args, "quiet", False) else None)

    runner = Runner(config, root)
    try:
        result = runner.tick()
    except Exception as exc:  # noqa: BLE001
        if not getattr(args, "quiet", False):
            print(f"[异常] {type(exc).__name__}: {exc}")
        return 1

    if not getattr(args, "quiet", False):
        label = {
            TickResult.IDLE_NOT_CAMPUS: "不在校园网，未做任何动作",
            TickResult.IDLE_OTHER_PORTAL: "当前网络不是本校认证网络，未做任何动作",
            TickResult.IDLE_NO_PORTAL: "未能确定门户地址",
            TickResult.IDLE_OFFLINE: "出口不通",
            TickResult.OPEN_NO_SESSION: "出口通畅，无需认证",
            TickResult.SESSION_UNKNOWN: "会话状态查询失败，本轮未介入",
            TickResult.BACKOFF: "退避等待中",
            TickResult.ONLINE: "已在线",
            TickResult.LOGGED_IN: "登录成功",
            TickResult.OCCUPIED: "出口 IP 被其他账号占用",
            TickResult.AUTH_FAILED: "认证失败",
            TickResult.NO_QUERY: "取不到认证参数",
            TickResult.ERROR: "执行异常",
        }[result]
        print(f"{result.value}: {label}")
        if runner.detail:
            print(f"  详情: {runner.detail}")

    if getattr(args, "check", False) and result.is_failure:
        return 1
    return 0


def cmd_daemon(args) -> int:
    config, path, root = _load(args)
    _prepare(config, root, console=False)

    problems = config.validate()
    for item in problems:
        print(f"[配置问题] {item}")

    lock = SingleInstance()
    if not lock.acquire():
        print("已有守护进程在运行，本次退出。")
        return 0

    runner = Runner(config, root)
    try:
        runner.run_forever()
    finally:
        lock.release()
    return 0


def cmd_status(args) -> int:
    config, path, root = _load(args)
    _prepare(config, root, level="WARNING")

    _hr("campus-netkeeper 运行状态")
    print(f"版本       : {__version__}")
    print(f"配置文件   : {path}")
    print(f"账号       : {config.username or '(未填写)'}  密码 {_mask(config.password)}")
    print(f"认证系统   : {config.adapter}")
    print(f"门户地址   : {config.portal_url or '(自动发现)'}")
    print(f"检测间隔   : {config.interval}s   保活间隔: {config.keepalive_interval or '关闭'}s")
    print(f"校园网段   : {', '.join(config.campus_ip_prefixes) or '(不限制)'}")
    print(f"校园 WiFi  : {', '.join(config.campus_ssids) or '(未配置)'}")
    print(f"无感认证   : {'启用' if config.enable_mac_auth else '停用'}")
    print("-" * 64)

    state = StateStore(root / config.state_file).load()
    print(f"上次执行   : {_ts(state.last_tick_at)}")
    print(f"上次登录   : {_ts(state.last_login_at)}")
    print(f"上次在线   : {_ts(state.last_online_at)}")
    print(f"上次保活   : {_ts(state.last_keepalive_at)}")
    print(f"会话索引   : {state.user_index or '-'}")
    print(f"无感认证   : {'已注册 ' + state.mac_registered_value if state.mac_registered else '未注册'}")
    print(f"探测缓存   : 适配器={state.adapter or '-'} 编码={state.encode_variant or '-'}")
    print(f"累计登录   : {state.login_attempts} 次   连续失败 {state.consecutive_failures} 次")
    if state.last_error:
        print(f"最近错误   : {state.last_error}")

    lock = SingleInstance()
    running = not lock.acquire()
    if not running:
        lock.release()
    print("-" * 64)
    print(f"守护进程   : {'运行中（daemon 模式）' if running else '未运行'}")
    return 0


def cmd_doctor(args) -> int:
    """逐项诊断，输出可直接贴进 Issue 的信息。"""
    config, path, root = _load(args)
    _prepare(config, root, level="WARNING")

    _hr("campus-netkeeper 诊断")
    print(f"版本       : {__version__}")
    print(f"平台       : {sys.platform}  Python {sys.version.split()[0]}")
    print(f"配置文件   : {path}")
    print("-" * 64)

    problems = config.validate()
    print(f"配置校验   : {'通过' if not problems else ' / '.join(problems)}")
    print(f"门户地址   : {config.portal_url or '(未配置)'}")
    print(f"校园 WiFi  : {netinfo.current_ssid() or '未连接/有线'}")
    print(f"默认网关   : {netinfo.default_gateway() or '未知'}")

    host = split_url(config.portal_url)[1] if config.portal_url else ""
    if host:
        from .transport import outbound_ip, tcp_open

        print(f"门户可达   : {'是' if tcp_open(host, 443, 3.0) else '否（443 不可达）'}")
        print(f"本机出口 IP: {outbound_ip(host) or '未知'}")
    print(f"TLS 级别   : {tls_profile_name()}")
    print("-" * 64)

    captive = probe.captive(config.probe_urls, timeout=min(config.http_timeout, 6.0))
    print(f"出口探测   : {captive.state.value} —— {captive.detail}")
    if captive.location:
        print(f"劫持地址   : {captive.location[:120]}")

    target: Optional[probe.PortalTarget] = None
    if captive.state is probe.CaptiveState.INTERCEPTED:
        base = probe.portal_base_from_url(captive.location)
        if base:
            target = probe.PortalTarget(base_url=base, raw_query=captive.query, source="强制门户")
    if target is None and config.portal_url:
        target = probe.PortalTarget(
            base_url=config.portal_url, raw_query=config.fallback_query, source="配置"
        )
    if target is None:
        print("\n[结论] 未能定位门户。请在校内运行本命令，或配置 portal_url。")
        return 1

    name, resolved = adapters.detect(target.base_url, timeout=config.http_timeout)
    print(f"识别结果   : {name or '未识别（将回退 ePortal）'}")
    if resolved:
        target = probe.PortalTarget(base_url=resolved, raw_query=target.raw_query, source=target.source)
    print(f"接口根路径 : {target.base_url}")
    if target.raw_query:
        print(f"认证参数   : {len(target.params)} 个 —— {', '.join(list(target.params)[:10])}")

    adapter = adapters.build(
        name or "eportal", target, timeout=config.http_timeout, options=config.adapter_options
    )
    for key, value in adapter.describe().items():
        print(f"{key:<11}: {value}")

    session = adapter.session()
    label = {
        SessionStatus.ONLINE: "在线",
        SessionStatus.OFFLINE: "离线（需要认证）",
        SessionStatus.UNKNOWN: "查询失败",
    }[session.status]
    print(f"会话状态   : {label}  账号={session.user_id or '-'}  索引={session.user_index or '-'}")
    if session.detail:
        print(f"服务端说明 : {session.detail}")
    print("-" * 64)
    print("[提示] 提交 Issue 时请附上以上全部输出（账号密码不会被打印）。")
    return 0


def cmd_login(args) -> int:
    config, path, root = _load(args)
    _prepare(config, root, console=True)

    problems = config.validate()
    if problems:
        for item in problems:
            print(f"[配置问题] {item}")
        return 2

    runner = Runner(config, root)
    result = runner.tick()
    if result is TickResult.ONLINE:
        print("当前已在线，无需重复登录。")
        return 0
    if result is TickResult.LOGGED_IN:
        print("登录成功。")
        return 0
    print(f"未登录：{result.value} —— {runner.detail}")
    return 1


def cmd_logout(args) -> int:
    config, path, root = _load(args)
    _prepare(config, root, console=True)

    state = StateStore(root / config.state_file).load()
    target = probe.PortalTarget(
        base_url=config.portal_url or state.portal_url, raw_query=config.fallback_query
    )
    if not target.base_url:
        print("不知道门户地址，无法下线。请先配置 portal_url。")
        return 2
    adapter = adapters.resolve_and_build(
        config.adapter, target, timeout=config.http_timeout, options=config.adapter_options
    )
    session = adapter.session()
    index = session.user_index or state.user_index
    if not index:
        print("本机没有可下线的会话。")
        return 1
    ok, detail = adapter.logout(index)
    print(f"{'下线成功' if ok else '下线失败'}：{detail}")
    return 0 if ok else 1


def cmd_mac_register(args) -> int:
    config, path, root = _load(args)
    _prepare(config, root, console=True)

    state = StateStore(root / config.state_file).load()
    target = probe.PortalTarget(
        base_url=config.portal_url or state.portal_url, raw_query=config.fallback_query
    )
    if not target.base_url:
        print("不知道门户地址。请先运行 `netkeeper init` 或配置 portal_url。")
        return 2

    adapter = adapters.resolve_and_build(
        config.adapter, target, timeout=config.http_timeout, options=config.adapter_options
    )
    if not adapter.supports_mac_auth:
        print(f"{adapter.label} 不支持 MAC 无感认证。")
        return 1

    session = adapter.session()
    if session.status is not SessionStatus.ONLINE:
        print("当前不在线，请先执行 `netkeeper login`。")
        return 1

    mac = session.mac or target.params.get("mac", "")
    if not mac:
        print("拿不到本机 MAC。请先运行 `netkeeper capture` 保存认证参数。")
        return 2

    print(f"待注册 MAC：{mac}")
    ok, detail = adapter.register_mac(mac, session.user_index)
    print(f"{'[成功] ' if ok else '[失败] '}{detail}")
    if ok:
        state.mac_registered = True
        state.mac_registered_value = mac
        state.user_index = session.user_index
        StateStore(root / config.state_file).save(state)
        return 0
    print("可能原因：学校未开放该功能，或该 MAC 已被其他账号绑定。")
    print("这不影响自动重登兜底。")
    return 1


def cmd_capture(args) -> int:
    config, path, root = _load(args)
    _prepare(config, root, level="WARNING")

    url = args.url
    if not url:
        print("请用浏览器打开一次校园网认证页（确保地址栏带 wlanuserip / mac 等参数），")
        print("然后把完整地址粘贴到下面：")
        try:
            url = input("URL> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消。")
            return 1

    query = probe.query_of(url)
    if not query:
        print("未能从该地址解析出查询参数。")
        return 2

    params = dict(urllib.parse.parse_qsl(query, keep_blank_values=True))
    print(f"解析到 {len(params)} 个参数")

    config.fallback_query = query
    base = probe.portal_base_from_url(url)
    if base and not config.portal_url:
        config.portal_url = base
        print(f"同时记录了门户地址：{base}")
    save_config(path, config)
    print(f"已写入 {path}")
    return 0


def cmd_wifi(args) -> int:
    config, path, root = _load(args)
    _prepare(config, root, level="WARNING")

    current = netinfo.current_ssid()
    print(f"当前无线 : {current or '未连接/有线'}")
    detail = netinfo.interface_detail()
    for key, label in (("name", "接口"), ("state", "状态"), ("mac", "物理地址"), ("signal", "信号")):
        value = detail.get(key)
        if value:
            print(f"{label:<9}: {value}")
    profiles = netinfo.saved_profiles()
    print(f"已保存配置: {', '.join(profiles) if profiles else '(无)'}")

    if args.connect:
        ok, detail = netinfo.connect(args.connect)
        print(f"\n连接「{args.connect}」: {'成功' if ok else '失败'} —— {detail}")
        return 0 if ok else 1
    return 0


# ------------------------------------------------------------------ 入口
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="netkeeper",
        description="通用校园网自动认证与保活工具（纯标准库，零依赖）",
        epilog="默认子命令为 tick：执行一次检测，适合交给系统调度器周期性调用。",
    )
    parser.add_argument("--config", help="配置文件路径")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    sub = parser.add_subparsers(dest="command")

    tick = sub.add_parser("tick", help="执行一次检测（默认，推荐配合系统调度器）")
    tick.add_argument("--quiet", action="store_true", help="不输出常规信息")
    tick.add_argument("--check", action="store_true", help="失败时返回非零退出码")

    tick_alias = sub.add_parser("once", help="tick 的别名")
    tick_alias.add_argument("--quiet", action="store_true")
    tick_alias.add_argument("--check", action="store_true")

    sub.add_parser("daemon", help="常驻循环（占内存，仅需秒级响应时使用）")
    sub.add_parser("init", help="交互式初始化")
    sub.add_parser("status", help="查看配置与运行状态")
    sub.add_parser("doctor", help="诊断环境与门户")
    sub.add_parser("login", help="立即认证一次")
    sub.add_parser("logout", help="主动下线")
    sub.add_parser("mac-register", help="注册 MAC 无感认证")

    capture = sub.add_parser("capture", help="保存认证参数（兜底）")
    capture.add_argument("--url", help="认证页完整地址；省略则交互式粘贴")

    wifi = sub.add_parser("wifi", help="查看无线状态或连接指定网络")
    wifi.add_argument("--connect", metavar="SSID", help="连接到指定 SSID")

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(errors="replace")  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001
                pass

    parser = build_parser()
    args = parser.parse_args(argv)

    handlers = {
        "tick": cmd_tick,
        "once": cmd_tick,
        "daemon": cmd_daemon,
        "init": cmd_init,
        "status": cmd_status,
        "doctor": cmd_doctor,
        "login": cmd_login,
        "logout": cmd_logout,
        "mac-register": cmd_mac_register,
        "capture": cmd_capture,
        "wifi": cmd_wifi,
    }
    handler = handlers.get(args.command or "tick", cmd_tick)

    try:
        return handler(args)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"[异常] {type(exc).__name__}: {exc}")
        if os.environ.get("NETKEEPER_DEBUG"):
            raise
        return 1
