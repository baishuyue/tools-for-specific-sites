# -*- coding: utf-8 -*-
"""命令行入口：login / status / run / dry-run / doctor / selftest / gui / task。"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
import time
from typing import Optional

from . import auth, protocol, store, track
from .client import SportClient, SportError
from .runner import AuthRequired, Runner, parse_rule


def _cfg(**over) -> dict:
    cfg = store.load_config()
    for key, val in over.items():
        if val is None:
            continue
        if key in ("sport_type", "distance_m", "duration_sec", "pace_variance", "steps_per_km"):
            cfg.setdefault("run", {})[key] = val
    return cfg


# ---------------------------------------------------------------------------
def cmd_login(args, log) -> int:
    """登录 / 导入凭据。默认走手机号+密码（自动处理滑块），也可导入 token。"""
    from . import login as login_mod

    cfg = store.load_config()

    # 1) 导入类
    if args.har:
        ok, msg = auth.from_har(args.har, log)
        return _after_import(ok, msg, cfg, log)
    if args.adb:
        ok, msg = auth.from_adb(serial=args.serial, adb=args.adb_path, log=log)
        return _after_import(ok, msg, cfg, log)
    if args.token:
        ok, msg = auth.from_manual(args.token, args.uid, args.unid)
        return _after_import(ok, msg, cfg, log)

    # 2) 密码登录
    phone = args.phone
    password = args.password
    if not phone and not password and not sys.stdin.isatty():
        print("非交互环境请用：login --phone 手机号 --password 密码")
        return 2
    if not phone:
        print("\n===== 运动世界校园 · 登录 =====")
        print("1) 手机号 + 密码登录（推荐：自动过滑块，无需抓包）")
        print("2) 从已登录的设备读取 token（adb，需 root）")
        print("3) 从抓包 HAR 导入")
        choice = input("请选择 [1]: ").strip() or "1"
        if choice == "2":
            ok, msg = auth.from_adb(log=log)
            return _after_import(ok, msg, cfg, log)
        if choice == "3":
            path = input("HAR 文件路径: ").strip().strip('"')
            ok, msg = auth.from_har(path, log)
            return _after_import(ok, msg, cfg, log)
        phone = input("手机号: ").strip()
    if not password:
        import getpass
        try:
            password = getpass.getpass("密码（输入时不显示）: ")
        except Exception:
            password = input("密码: ")

    try:
        info = login_mod.login_with_password(
            phone, password, cfg, log=log,
            captcha_mode=args.captcha,
            captcha_key=args.captcha_key,
            open_browser=not args.no_browser,
            direct_first=not args.no_direct,
            debug=not args.no_debug,
        )
    except login_mod.LoginError as exc:
        print("登录失败：%s" % exc)
        return 1
    cfg.setdefault("login", {})["phone"] = phone
    store.save_config(cfg)
    print("\n登录成功：%s（%s）%s / %s" % (
        info.get("name"), info.get("username"),
        info.get("campusName") or info.get("universityName") or "",
        info.get("gradeClass") or ""))
    print("uid=%s  unid=%s  凭据已加密保存，可直接运行：一键跑步.bat run" % (
        info.get("uid"), info.get("unid")))
    return 0


def _after_import(ok: bool, msg: str, cfg: dict, log) -> int:
    print(msg)
    if not ok:
        return 1
    try:
        info = auth.whoami(cfg, log)
        print("账号校验通过：%s（uid=%s unid=%s %s）" % (
            info.get("name"), info.get("uid"), info.get("unid"),
            info.get("campusName") or info.get("universityName") or ""))
    except SportError as exc:
        print("账号校验失败：%s" % exc)
        return 1
    return 0


def cmd_diagnose(args, log) -> int:
    """只读诊断：对时 + checkGeeUse 原始响应（不登录、不过滑块、不发短信）。"""
    from . import login as login_mod

    cfg = store.load_config()
    phone = (args.phone or (cfg.get("login") or {}).get("phone") or "").strip()
    if not phone:
        print("用法：一键跑步.bat diagnose --phone 手机号")
        return 2
    client = login_mod._client(cfg, log)
    t0 = client.now_ms()
    server = client.sync_time()
    print("本地时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t0 / 1000)))
    print("服务端时间：%s（偏差 %.1f 秒）" % (
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(server / 1000)),
        (server - t0) / 1000.0))
    import uuid as _uuid
    session = str(_uuid.uuid4())
    raw = login_mod.check_gee_use(client, phone, session)
    print("checkGeeUse 原始响应：")
    print(json.dumps(raw, ensure_ascii=False, indent=1))
    cid = login_mod.extract_captcha_id(raw)
    print("\n识别到的 captchaId：%s" % (cid or "（服务端未下发，使用内置 3b02ad39bd099fd3a8336d9347a189ab）"))
    print("本次会话 uuid：%s" % session)
    return 0


def cmd_accounts(args, log) -> int:
    """列出本机已登录账号（token 已 DPAPI 加密）。"""
    rows = store.list_accounts()
    if not rows:
        print("本机还没有账号：一键跑步.bat login --phone 手机号 --password 密码")
        return 1
    print("%-3s %-12s %-12s %-8s %-10s %s" % ("", "uid", "手机号", "unid", "姓名", "更新时间"))
    for i, a in enumerate(rows, 1):
        print("%-3s %-12s %-12s %-8s %-10s %s" % (
            "*" if a.get("current") else str(i), a.get("uid") or a.get("key"),
            a.get("phone") or "-", a.get("unid") or "-",
            (a.get("name") or "-")[:6], a.get("updated_at") or "-"))
    print("\n当前账号：%s；切换：一键跑步.bat switch <uid 或 手机号>" % (store.current_key() or "-"))
    return 0


def cmd_switch(args, log) -> int:
    key = (args.key or "").strip()
    if not key:
        return cmd_accounts(args, log)
    if not store.set_account(key):
        # 允许用手机号匹配
        hit = [a for a in store.list_accounts()
               if key in (str(a.get("phone") or ""), str(a.get("uid") or ""), str(a.get("key") or ""))]
        if not hit or not store.set_account(str(hit[0].get("key"))):
            print("没有找到账号：%s" % key)
            return 1
        key = str(hit[0].get("key"))
    acc = store.load_credentials(key)
    print("已切换到：%s（uid=%s，%s）" % (acc.get("name") or "-", acc.get("uid"), acc.get("campus") or "-"))
    return 0


def cmd_status(args, log) -> int:
    """账号 / 今日记录 / 服务端判定 / 学期里程（全部来自服务端）。"""
    from .runner import format_verify, verify_now

    cfg = store.load_config()
    account = getattr(args, "account", None)
    cred = store.load_credentials(account)
    if not cred.get("token"):
        print("尚未登录：一键跑步.bat login --phone 手机号 --password 密码")
        return 1

    print("== 数据核对（服务端真实数据） ==")
    print("账号：%s（uid=%s，%s）" % (cred.get("name") or "-", cred.get("uid"), cred.get("campus") or "-"))
    v = verify_now(cfg, log, account=account)
    if v.get("error"):
        print("核对失败：%s" % v["error"])
        return 1
    for line in format_verify(v):
        print(line)

    net = cfg.get("net") or {}
    client = SportClient(uid=cred.get("uid"), token=cred.get("token"), unid=cred.get("unid"),
                         device=store.device_for(cfg, cred), timeout=float(net.get("timeout", 15)),
                         retries=int(net.get("retries", 3)), proxy=net.get("proxy") or None,
                         insecure=bool(net.get("insecure", False)), log=log)
    try:
        client.sync_time()
        recs = client.records(page=1, size=6)
        if recs:
            print("\n最近记录：")
            for r in recs:
                ts = datetime.datetime.fromtimestamp(int(r.get("startTime") or 0) / 1000.0)
                ok = bool(r.get("complete"))
                print("  %s  %.2fkm  %ds  %s  rrid=%s" % (
                    ts.strftime("%m-%d %H:%M"), float(r.get("totalDis") or 0) / 1000.0,
                    int(r.get("totalTime") or 0), "有效" if ok else "无效", r.get("rrid")))
        meta = client.semester_meta()
        if meta:
            print("\n学期：%s（sid=%s，%s ~ %s）" % (
                meta.get("sname"),
                meta.get("sid"),
                datetime.datetime.fromtimestamp(int(meta.get("startDate") or 0) / 1000.0).strftime("%Y-%m-%d"),
                datetime.datetime.fromtimestamp(int(meta.get("endDate") or 0) / 1000.0).strftime("%Y-%m-%d")))
    except SportError as exc:
        print("查询失败：%s" % exc)
        return 1
    return 0


def cmd_verify(args, log) -> int:
    return cmd_status(args, log)


def cmd_run(args, log) -> int:
    cfg = _cfg(sport_type=args.sport_type, distance_m=args.distance,
               duration_sec=args.duration)
    try:
        runner = Runner(cfg, log, account=getattr(args, "account", None))
    except AuthRequired as exc:
        print(exc)
        return 1
    try:
        rep = runner.run(dry=args.dry_run, force=args.force, poll_sec=args.poll,
                         stop_before_submit=args.stop_before_submit)
    except SportError as exc:
        if str(getattr(exc, "code", "")) == "401" or "401" in str(exc):
            print("失败：登录已失效，请重新登录：一键跑步.bat login")
        else:
            print("失败：%s" % exc)
        return 1
    except track.TrackCheck as exc:
        print("本地校验未通过：%s（已中止，未上报）" % exc)
        return 1
    print("")
    if rep.status == "blocked":
        print("已阻止提交：%s" % rep.message)
        print("如确认要强行提交（风险自负）：一键跑步.bat run --force")
        return 3
    print("结果：%s  %s" % (rep.status, rep.message))
    if rep.rrid:
        print("rrid=%s  里程=%.0fm  用时=%ds  配速=%s  步数=%s  打卡点=%s" % (
            rep.rrid, rep.distance_m, rep.duration_s, rep.pace, rep.steps, rep.checkpoints))
    for w in rep.warnings:
        print("[警告] " + w)
    return 0 if rep.ok else (2 if rep.status in ("pending",) else 3)


def cmd_doctor(args, log) -> int:
    ok = True
    print("== 环境自检 ==")
    print("[OK] Python %s" % sys.version.split()[0])
    if not protocol.self_test(verbose=True):
        ok = False
        print("[FAIL] 密码学自检未通过")
    cfg = store.load_config()
    print("[OK] 配置：%s（DeviceId=%s）" % (store.CONFIG_PATH, cfg["device"].get("DeviceId")))
    cred = store.load_credentials()
    if cred.get("token"):
        print("[OK] 凭据：source=%s uid=%s unid=%s（DPAPI 加密存储）" % (
            cred.get("source"), cred.get("uid"), cred.get("unid")))
    else:
        ok = False
        print("[缺失] 凭据：请 login 导入 token")
    try:
        store.LOG_DIR.mkdir(parents=True, exist_ok=True)
        (store.LOG_DIR / ".w").write_text("1")
        (store.LOG_DIR / ".w").unlink()
        print("[OK] 目录可写：%s" % store.BASE_DIR)
    except Exception as exc:
        ok = False
        print("[FAIL] 目录不可写：%s" % exc)
    if not args.offline:
        net = cfg.get("net") or {}
        client = SportClient(uid=cred.get("uid"), token=cred.get("token"), unid=cred.get("unid"),
                             device=cfg.get("device"), timeout=10, retries=1,
                             proxy=net.get("proxy") or None, insecure=bool(net.get("insecure", False)))
        try:
            t = client.sync_time()
            print("[OK] 网关连通，服务端时间 %s" % datetime.datetime.fromtimestamp(t / 1000).strftime("%F %T"))
        except SportError as exc:
            ok = False
            print("[FAIL] 网关不可达：%s" % exc)
        if cred.get("token"):
            try:
                info = client.user_info()
                print("[OK] token 有效：%s（uid=%s）" % (info.get("name"), info.get("uid")))
            except SportError as exc:
                ok = False
                print("[FAIL] token 校验失败：%s" % exc)
    print("\n自检结果：%s" % ("全部就绪" if ok else "存在待处理项"))
    return 0 if ok else 1


def cmd_selftest(args, log) -> int:
    from . import selftest
    return 0 if selftest.run() else 1


def cmd_task(args, log) -> int:
    py = sys.executable
    script = str(store.BASE_DIR / "sport_run.py")
    name = "SportWorldAutoRun"
    cmd = '"%s" "%s" run --quiet' % (py, script)
    if args.action == "remove":
        subprocess.run(["schtasks", "/Delete", "/TN", name, "/F"], capture_output=True)
        print("已删除计划任务 %s" % name)
        return 0
    when = args.time or "06:10"
    r = subprocess.run(["schtasks", "/Create", "/TN", name, "/TR", cmd,
                        "/SC", "DAILY", "/ST", when, "/F"], capture_output=True, text=True)
    print(r.stdout or r.stderr)
    return 0 if r.returncode == 0 else 1


def cmd_gui(args, log) -> int:
    from . import gui
    gui.launch()
    return 0


# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sport_run.py", description="运动世界校园 · 一键跑步（纯 Windows，手机号密码直接登录）")
    sub = p.add_subparsers(dest="cmd")

    q = sub.add_parser("gui", help="图形界面（默认）")
    q.set_defaults(func=cmd_gui)

    q = sub.add_parser("login", help="登录（手机号+密码，自动过滑块）/ 导入凭据")
    q.add_argument("--phone", help="手机号")
    q.add_argument("--password", help="密码（不传则交互输入）")
    q.add_argument("--captcha", choices=["browser", "api"], help="滑块方式：browser=浏览器拖一次（默认），api=打码平台")
    q.add_argument("--captcha-key", help="打码平台 key（2captcha 兼容）")
    q.add_argument("--no-browser", action="store_true", help="不自动打开浏览器，只打印本地验证页地址")
    q.add_argument("--no-direct", action="store_true", help="跳过\"先直接登录\"，强制先过滑块")
    q.add_argument("--no-debug", action="store_true", help="关闭 logs/login-debug-*.json 诊断文件")
    q.add_argument("--token"); q.add_argument("--uid"); q.add_argument("--unid")
    q.add_argument("--har", help="从 HAR 抓包里提取 token")
    q.add_argument("--adb", action="store_true", help="从 adb 设备读取 token（需 root）")
    q.add_argument("--adb-path"); q.add_argument("--serial")
    q.set_defaults(func=cmd_login)

    q = sub.add_parser("accounts", help="列出本机已登录的账号")
    q.set_defaults(func=cmd_accounts)

    q = sub.add_parser("switch", help="切换当前账号")
    q.add_argument("key", nargs="?", help="uid 或手机号")
    q.set_defaults(func=cmd_switch)

    q = sub.add_parser("diagnose", help="只读诊断：对时 + checkGeeUse 原始响应")
    q.add_argument("--phone", help="手机号")
    q.set_defaults(func=cmd_diagnose)

    q = sub.add_parser("status", help="数据核对：今日记录 / 服务端判定 / 学期里程")
    q.add_argument("--account", help="指定账号（uid 或手机号）")
    q.set_defaults(func=cmd_status)

    q = sub.add_parser("verify", help="同 status（只核对，不跑）")
    q.set_defaults(func=cmd_verify)

    q = sub.add_parser("run", help="执行一键跑步")
    q.add_argument("--dry-run", action="store_true", help="只生成报文不提交")
    q.add_argument("--force", action="store_true", help="忽略当日额度与时段限制")
    q.add_argument("--sport-type", type=int, choices=[1, 6])
    q.add_argument("--distance", type=int)
    q.add_argument("--duration", type=int)
    q.add_argument("--poll", type=int, default=600, help="核验轮询上限秒数")
    q.add_argument("--quiet", action="store_true", help="静默（计划任务用）")
    q.add_argument("--stop-before-submit", action="store_true",
                   help="只做到轨迹上传，不提交结算（排障用）")
    q.add_argument("--account", help="用哪个账号跑（uid 或手机号）；默认当前账号")
    q.set_defaults(func=cmd_run)

    q = sub.add_parser("doctor", help="环境自检")
    q.add_argument("--offline", action="store_true")
    q.set_defaults(func=cmd_doctor)

    q = sub.add_parser("selftest", help="离线全链路自测")
    q.set_defaults(func=cmd_selftest)

    q = sub.add_parser("task", help="安装/删除每日计划任务")
    q.add_argument("action", choices=["install", "remove"])
    q.add_argument("--time", help="HH:MM，默认 06:10")
    q.set_defaults(func=cmd_task)
    return p


def main(argv: Optional[list] = None) -> int:
    store.ensure_utf8_console()
    args = build_parser().parse_args(argv)
    if not getattr(args, "func", None):
        args = build_parser().parse_args(["gui"])
    log = store.Logger(echo=not getattr(args, "quiet", False))
    try:
        return int(args.func(args, log) or 0)
    except KeyboardInterrupt:
        print("\n已取消")
        return 130
    except Exception as exc:                     # 兜底：任何异常都落到日志里
        log("异常：%r" % exc)
        if not getattr(args, "quiet", False):
            import traceback
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
