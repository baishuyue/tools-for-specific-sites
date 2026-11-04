# -*- coding: utf-8 -*-
"""凭据获取：密码登录 / token 导入（HAR、adb、手工）。

三种方式覆盖所有场景：
  * password  —— 走 checkGeeUse + 密码登录；设备被风控时需要滑块，此时自动提示改用导入；
  * har       —— 从已有抓包（.har）里抠 token/uid/unid，完全离线；
  * adb       —— 从已登录的手机/模拟器 shared_prefs 里读 token（一次性，有 root 即可）；
  * manual    —— 直接粘贴。
拿到一次之后写进 credentials.bin（DPAPI），后续所有操作都在 Windows 本地完成。
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import uuid
from typing import Any, Callable, Optional, Tuple

from . import store
from .client import HOST_RUN, SportClient, SportError

EP_CHECK_GEE = HOST_RUN + "/api/v65/security/checkGeeUse"
EP_LOGIN = HOST_RUN + "/api/v70100/login"
PKG = "com.zjwh.android_wh_physicalfitness"


# ---------------------------------------------------------------------------
# 密码登录（委托给 login 模块，保留旧接口名）
# ---------------------------------------------------------------------------
def password_login(phone: str, password: str, cfg: dict,
                   log: Callable[[str], None] = print,
                   captcha_mode: Optional[str] = None,
                   captcha_key: Optional[str] = None,
                   open_browser: bool = True) -> Tuple[bool, str, dict]:
    from . import login as login_mod
    try:
        info = login_mod.login_with_password(phone, password, cfg, log=log,
                                             captcha_mode=captcha_mode,
                                             captcha_key=captcha_key,
                                             open_browser=open_browser)
        return True, "登录成功：%s（uid=%s）" % (info.get("name"), info.get("uid")), info
    except login_mod.LoginError as exc:
        return False, "登录失败：%s" % exc, {}


# ---------------------------------------------------------------------------
# HAR 导入
# ---------------------------------------------------------------------------
def _har_entries(har: dict) -> list:
    return ((har.get("log") or {}).get("entries")) or []


def from_har(path: str, log: Callable[[str], None] = print) -> Tuple[bool, str]:
    try:
        har = json.loads(open(path, "r", encoding="utf-8", errors="replace").read())
    except Exception as exc:
        return False, "读取 HAR 失败：%s" % exc
    best: dict = {}
    for entry in _har_entries(har):
        req = entry.get("request") or {}
        url = req.get("url") or ""
        if "iydsj.com" not in url:
            continue
        headers = {h.get("name", "").lower(): h.get("value", "") for h in req.get("headers") or []}
        token = headers.get("token") or ""
        uid = headers.get("uid") or ""
        if not token or token in ("", "-1"):
            continue
        unid = ""
        text = ((entry.get("response") or {}).get("content") or {}).get("text") or ""
        if text:
            try:
                blob = json.loads(text)
                data = blob.get("data") if isinstance(blob, dict) else None
                if isinstance(data, dict):
                    unid = str(data.get("unid") or "")
                    uid = uid or str(data.get("uid") or "")
            except Exception:
                pass
        if not unid:
            m = re.search(r'"unid"\s*:\s*(\d+)', text or "")
            if m:
                unid = m.group(1)
        best = {"uid": uid, "token": token, "unid": unid}
        if unid and uid:
            break
    if not best.get("token"):
        return False, "HAR 里没找到带 token 的 iydsj 请求；请确认抓的是运动世界 App 的流量"
    store.save_credentials(uid=best.get("uid"), token=best["token"],
                           unid=best.get("unid"), source="har")
    log("已从 HAR 导入：uid=%s unid=%s token=%s..." % (best.get("uid"), best.get("unid"), best["token"][:12]))
    return True, "导入成功"


# ---------------------------------------------------------------------------
# adb 导入（一次性）
# ---------------------------------------------------------------------------
def _find_adb(user_path: Optional[str] = None) -> Optional[str]:
    if user_path and os.path.exists(user_path):
        return user_path
    for name in ("adb", "adb.exe"):
        hit = shutil.which(name)
        if hit:
            return hit
    for base in (r"C:\Program Files\Netease\MuMu\nx_main",
                 r"D:\Program Files\Netease\MuMu\nx_main",
                 r"C:\Program Files\Netease\MuMuPlayer-12.0\shell"):
        cand = os.path.join(base, "adb.exe")
        if os.path.exists(cand):
            return cand
    return None


def from_adb(serial: Optional[str] = None, adb: Optional[str] = None,
             log: Callable[[str], None] = print) -> Tuple[bool, str]:
    exe = _find_adb(adb)
    if not exe:
        return False, "找不到 adb.exe，请用 --adb 指定路径"
    base = [exe] + (["-s", serial] if serial else [])
    prefs = "/data/data/%s/shared_prefs" % PKG
    cmds = [
        ["shell", "su", "-c", "ls %s" % prefs],
    ]
    try:
        out = subprocess.run(base + cmds[0], capture_output=True, text=True, timeout=30)
    except Exception as exc:
        return False, "adb 调用失败：%s" % exc
    names = [n.strip() for n in (out.stdout or "").splitlines() if n.strip().endswith(".xml")]
    if not names:
        return False, ("读取 shared_prefs 失败（需要 root/已授权 su）：%s" % (out.stderr or "").strip()[:160])
    merged = ""
    for name in names:
        r = subprocess.run(base + ["shell", "su", "-c", "cat %s/%s" % (prefs, name)],
                           capture_output=True, text=True, timeout=30)
        merged += (r.stdout or "")
    token = _pref(merged, "token")
    uid = _pref(merged, "uid") or _pref(merged, "userId")
    unid = _pref(merged, "unid")
    if not token:
        return False, "shared_prefs 里没找到 token，请在 App 内确认已登录"
    store.save_credentials(uid=uid, token=token, unid=unid, source="adb")
    log("已从设备导入：uid=%s unid=%s token=%s..." % (uid, unid, token[:12]))
    return True, "导入成功"


def _pref(xml: str, key: str) -> str:
    m = re.search(r'name="%s"[^>]*>([^<]*)<' % re.escape(key), xml)
    return m.group(1).strip() if m else ""


# ---------------------------------------------------------------------------
# 手工
# ---------------------------------------------------------------------------
def from_manual(token: str, uid: Any = None, unid: Any = None) -> Tuple[bool, str]:
    if not token:
        return False, "token 不能为空"
    store.save_credentials(uid=uid, token=token, unid=unid, source="manual")
    return True, "已保存"


def whoami(cfg: dict, log: Callable[[str], None] = print) -> dict:
    cred = store.load_credentials()
    if not cred.get("token"):
        raise SportError("本地没有凭据，请先 login")
    net = cfg.get("net") or {}
    client = SportClient(uid=cred.get("uid"), token=cred.get("token"), unid=cred.get("unid"),
                         device=store.device_for(cfg, cred), timeout=float(net.get("timeout", 15)),
                         retries=int(net.get("retries", 3)), proxy=net.get("proxy") or None,
                         insecure=bool(net.get("insecure", False)), log=log)
    client.sync_time()
    info = client.user_info()
    store.save_credentials(uid=info.get("uid"), unid=info.get("unid"),
                           source=cred.get("source", "manual"),
                           phone=str(cred.get("phone") or ""),
                           name=str(info.get("name") or ""),
                           campus=str(info.get("campusName") or info.get("universityName") or ""))
    return info
