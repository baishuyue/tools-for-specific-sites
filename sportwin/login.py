# -*- coding: utf-8 -*-
"""账号登录：先直接登，需要再补极验（自适应），全链路可 dump。

为什么改成"先直接登录"：
  `checkGeeUse` 的语义在不同版本/账号上并不一致（实测同一个手机号，
  `type=1/2` 返回 data:false，`type=0/3` 直接报错）。把它当"必须验证"会把
  原本能直接登录的会话卡在滑块上（服务端从没给这个会话准备 gee 校验，
  geevalidate 自然回 false）。

现在的顺序：
  1) checkGeeUse 只做记录，不阻断；
  2) 直接 POST /api/v70100/login；
     - 成功 → 结束（大多数情况走到这里）；
     - 返回"密码错"（10003/10004/11307）→ 直接报错，不再折腾滑块；
     - 返回其它错误 → 认为服务端要求人机验证，进入 3；
  3) 极验（浏览器拖一次 / 打码平台）→ geevalidate → 再登录一次。
"""
from __future__ import annotations

import base64
import json
import re
import time
import uuid
from typing import Any, Callable, Dict, Optional, Tuple

from . import captcha as captcha_mod
from . import store
from .client import HOST_RUN, SportClient, SportError

EP_CHECK_GEE = HOST_RUN + "/api/v65/security/checkGeeUse"
EP_GEE_VALIDATE = HOST_RUN + "/api/v70270/security/geevalidate"
EP_LOGIN = HOST_RUN + "/api/v70100/login"

#: 登录错误码 → 人话
ERROR_HINTS = {
    10003: "账号或密码不正确（服务端：验证失败）",
    10004: "账号或密码不正确",
    11307: "账号或密码校验不一致（密码错误，或账号未注册）",
    10175: "账号或密码错误，请重新输入",
    18001: "该账号需要先在手机 App 内完成设备绑定",
    18002: "换设备登录需要在手机 App 内确认",
}
#: 这些码代表"凭据本身不对"，再滑块也没用
PASSWORD_CODES = {10003, 10004, 10175, 11307, 18001, 18002}


class LoginError(RuntimeError):
    def __init__(self, message: str, code: Any = -1, raw: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.raw = raw


def _client(cfg: dict, log: Callable[[str], None], phone: str = "") -> SportClient:
    net = cfg.get("net") or {}
    acc = store.find_account(phone) if phone else None
    device = store.device_for(cfg, acc or ({"phone": phone} if phone else None))
    return SportClient(device=device, timeout=float(net.get("timeout", 15)),
                       retries=max(2, int(net.get("retries", 3))),
                       proxy=net.get("proxy") or None,
                       insecure=bool(net.get("insecure", False)), log=log)


def _short(obj: Any, n: int = 400) -> str:
    try:
        text = json.dumps(obj, ensure_ascii=False)
    except Exception:
        text = str(obj)
    return text[:n]


_HEX32 = re.compile(r"^[0-9a-f]{32}$")


def extract_captcha_id(obj: Any) -> str:
    """服务端有时会在 checkGeeUse 里带出当前版本用的极验 captchaId，能拿到就用它。"""
    if isinstance(obj, dict):
        for key, val in obj.items():
            if isinstance(val, str) and _HEX32.match(val.strip().lower()) and \
                    ("captcha" in key.lower() or key.lower() in ("gt", "id", "cid")):
                return val.strip().lower()
        for val in obj.values():
            got = extract_captcha_id(val)
            if got:
                return got
    elif isinstance(obj, list):
        for val in obj:
            got = extract_captcha_id(val)
            if got:
                return got
    return ""


_SECRET_KEYS = {"token", "password", "authorization", "headersign", "tokensign", "pass_token"}


def _redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: ("***" if str(k).lower() in _SECRET_KEYS else _redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(x) for x in obj]
    return obj


class _Dump:
    """把登录每一步的原始请求/响应落盘，方便定位。"""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self.data: Dict[str, Any] = {"at": time.strftime("%Y-%m-%d %H:%M:%S")}

    def add(self, key: str, value: Any) -> None:
        if self.enabled:
            self.data[key] = _redact(value)

    def flush(self) -> str:
        if not self.enabled:
            return ""
        store.LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = store.LOG_DIR / ("login-debug-%s.json" % time.strftime("%Y%m%d-%H%M%S"))
        try:
            path.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
            return str(path)
        except Exception:
            return ""


# ---------------------------------------------------------------------------
# 单步
# ---------------------------------------------------------------------------
def check_gee_use(client: SportClient, phone: str, session: str) -> dict:
    """只观测，不据此阻断登录。返回原始 JSON。"""
    body = {"username": phone, "uuid": session, "unid": 0, "type": 2}
    try:
        raw = client._send("POST", EP_CHECK_GEE, body)
    except SportError as exc:
        return {"error": getattr(exc, "code", -1), "message": str(exc)}
    return raw if isinstance(raw, dict) else {"raw": raw}


def submit_login(client: SportClient, phone: str, password: str, session: str) -> Tuple[Optional[dict], dict]:
    """返回 (成功数据 or None, 原始响应)。不抛业务异常，交给调用方判定。"""
    basic = base64.b64encode(("%s:%s" % (phone, password)).encode("utf-8")).decode("ascii")
    body = {
        "loginType": 1,
        "device_model": (client.device or {}).get("deviceName", "Android Device"),
        "os_version": (client.device or {}).get("osVersion", "13"),
        "username": phone,
        "uuid": session,
    }
    try:
        raw = client._send("POST", EP_LOGIN, body, extra_headers={"Authorization": "Basic " + basic})
    except SportError as exc:
        raw = getattr(exc, "raw", None) or {"error": getattr(exc, "code", -1), "message": str(exc)}
    if not isinstance(raw, dict):
        return None, {"raw": raw}
    data = raw.get("data")
    if raw.get("error") == 10000 and isinstance(data, dict) and data.get("token"):
        return data, raw
    return None, raw


def gee_validate(client: SportClient, res: "captcha_mod.GeetestResult",
                 session: str, phone: str) -> Tuple[bool, dict]:
    payload = res.validate_payload(session, phone)
    try:
        raw = client._send("POST", EP_GEE_VALIDATE, payload)
    except SportError as exc:
        return False, {"error": getattr(exc, "code", -1), "message": str(exc),
                       "_payload": payload}
    if not isinstance(raw, dict):
        return False, {"raw": raw, "_payload": payload}
    # 服务端成功时返回 {"error":10000,"data":""}（空串），失败才是 10003 / data:false。
    # 之前按 data 的真值判断，把空串当成失败，才会"滑完又让滑"。
    ok = raw.get("error") == 10000
    out = dict(raw)
    out["_payload"] = payload
    return ok, out


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def login_with_password(
    phone: str,
    password: str,
    cfg: dict,
    log: Callable[[str], None] = print,
    captcha_mode: Optional[str] = None,
    captcha_key: Optional[str] = None,
    open_browser: bool = True,
    direct_first: bool = True,
    debug: bool = True,
) -> Dict[str, Any]:
    """完整登录。成功返回账号信息（已写入 credentials.bin）。"""
    phone = (phone or "").strip()
    if not (phone.isdigit() and len(phone) == 11):
        raise LoginError("手机号格式不对：%s" % phone)
    if not password:
        raise LoginError("密码不能为空")

    login_cfg = cfg.get("login") or {}
    mode = captcha_mode or login_cfg.get("captcha_mode") or "browser"
    key = captcha_key or login_cfg.get("captcha_api_key") or ""
    timeout = float(login_cfg.get("captcha_timeout", 300))
    captcha_id = login_cfg.get("captcha_id") or captcha_mod.CAPTCHA_ID
    dump = _Dump(debug)

    client = _client(cfg, log, phone=phone)
    session = str(uuid.uuid4())
    log("① 与服务端对时 ...")
    client.sync_time()

    log("② 账号安全初审（checkGeeUse，仅观测）...")
    raw_check = check_gee_use(client, phone, session)
    dump.add("checkGeeUse", raw_check)
    log("   %s" % _short(raw_check, 200))
    server_cid = extract_captcha_id(raw_check)
    if server_cid and server_cid != captcha_id:
        log("   服务端下发了 captchaId=%s，已改用该值" % server_cid)
        captcha_id = server_cid

    # ---- 第一步：直接登录 ----
    data: Optional[dict] = None
    raw_login: dict = {}
    first_err: Any = None
    if direct_first:
        log("③ 尝试直接登录 ...")
        data, raw_login = submit_login(client, phone, password, session)
        dump.add("login#1", raw_login)
        if data:
            log("   直接登录成功（服务端本次未要求人机验证）")
        else:
            first_err = raw_login.get("error")
            log("   未通过：%s %s" % (first_err, _short(raw_login.get("message"), 120)))
            # 10175/10003 既可能是密码错，也可能只是"这个会话还没过人机验证"。
            # 两种情况都很常见，所以带一次验证再判一次：过完还是同样的错，才是密码错。
            if first_err in PASSWORD_CODES:
                log("   （可能是密码不对，也可能只是需要先过人机验证，带验证再试一次）")

    # ---- 第二步：极验 + 再登录 ----
    if not data:
        last_raw: dict = {}
        max_attempts = 1 if first_err in PASSWORD_CODES else 3
        for attempt in range(1, max_attempts + 1):
            log("④ 需要人机验证（第 %d/%d 次）..." % (attempt, max_attempts))
            try:
                res = captcha_mod.solve(mode=mode, api_key=key, captcha_id=captcha_id,
                                        timeout=timeout, open_browser=open_browser, log=log)
            except captcha_mod.CaptchaError as exc:
                raise LoginError("人机验证未完成：%s" % exc)
            dump.add("captcha#%d" % attempt, {
                "lot_number": res.lot_number,
                "len_captcha_output": len(res.captcha_output or ""),
                "len_pass_token": len(res.pass_token or ""),
                "gen_time": res.gen_time,
            })
            if not (res.captcha_output and res.pass_token and res.gen_time):
                log("   [警告] 滑块结果字段不完整（output=%d, pass=%d），可能被服务端拒绝" % (
                    len(res.captcha_output or ""), len(res.pass_token or "")))

            log("⑤ 上报极验凭据（geevalidate）...")
            ok, raw_val = gee_validate(client, res, session, phone)
            last_raw = raw_val
            dump.add("geevalidate#%d" % attempt, raw_val)
            log("   响应：%s" % _short({k: v for k, v in raw_val.items() if k != "_payload"}, 240))
            if ok:
                log("   验证通过，重新提交账号密码 ...")
                data, raw_login = submit_login(client, phone, password, session)
                dump.add("login#%d" % (attempt + 1), raw_login)
                if data:
                    break
                err = raw_login.get("error")
                log("   仍未登录成功：%s %s" % (err, _short(raw_login.get("message"), 120)))
                if err in PASSWORD_CODES:
                    dump.flush()
                    raise LoginError("%s（code=%s）" % (ERROR_HINTS.get(err, "账号或密码不正确"), err),
                                     err, raw_login)
            else:
                log("   这次没通过，重试 ...")

        if not data:
            path = dump.flush()
            hint = ""
            if last_raw.get("error") not in (None, 10000):
                hint = "；服务端错误 %s" % last_raw.get("error")
            raise LoginError(
                "登录失败：人机验证未被服务端接受%s。"
                "已保存诊断文件：%s（把它发给开发者可定位）" % (hint, path or "（未开启 debug）"),
                last_raw.get("error"), last_raw)

    # ---- 落盘 + 自检 ----
    log("⑥ 保存凭据并用 token 自检 ...")
    store.save_credentials(uid=data.get("uid"), token=data.get("token"),
                           unid=data.get("unid"), source="password", phone=phone,
                           name=str(data.get("name") or ""),
                           campus=str(data.get("campusName") or data.get("universityName") or ""))
    client.set_credentials(uid=data.get("uid"), token=data.get("token"), unid=data.get("unid"))
    info = client.user_info()
    store.save_credentials(uid=info.get("uid"), unid=info.get("unid"), source="password",
                           phone=phone, name=str(info.get("name") or ""),
                           campus=str(info.get("campusName") or info.get("universityName") or ""))
    dump.add("user_info", {k: info.get(k) for k in ("uid", "unid", "name", "username", "campusName")})
    path = dump.flush()
    if path:
        log("   诊断文件：%s" % path)
    log("   凭据已用 DPAPI 加密保存")
    return info


def try_silent_login(cfg: dict, log: Callable[[str], None] = print) -> Optional[dict]:
    """本地已有 token 时先验证一次，有效就直接用。"""
    cred = store.load_credentials()
    if not cred.get("token"):
        return None
    client = _client(cfg, log, phone=str(cred.get("phone") or ""))
    client.set_credentials(uid=cred.get("uid"), token=cred.get("token"), unid=cred.get("unid"))
    try:
        client.sync_time()
        info = client.user_info()
    except SportError:
        return None
    store.save_credentials(uid=info.get("uid"), unid=info.get("unid"),
                           source=cred.get("source", "manual"),
                           phone=str(cred.get("phone") or ""),
                           name=str(info.get("name") or ""),
                           campus=str(info.get("campusName") or info.get("universityName") or ""))
    return info


def ensure_login(cfg: dict, phone: str = "", password: str = "",
                 log: Callable[[str], None] = print, **kw) -> dict:
    info = try_silent_login(cfg, log)
    if info:
        log("本地凭据仍然有效：%s（%s）" % (info.get("name"), info.get("username")))
        return info
    if not phone or not password:
        raise LoginError("本地凭据失效，需要重新登录（提供手机号 + 密码）")
    return login_with_password(phone, password, cfg, log=log, **kw)
