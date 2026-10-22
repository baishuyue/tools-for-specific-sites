"""深澜（Srun）适配器。

深澜是国内另一套大规模部署的校园网认证系统（H3C / 深澜软件）。它与
ePortal 族完全不同：四次 JSONP 风格 GET 请求，带一次性 challenge，
密码走 HMAC-MD5，用户信息走 XXTEA 变体加密后再用**置换过字母表的**
Base64 编码。

协议流程::

    1. GET /cgi-bin/rad_user_info     查询在线状态
    2. GET /srun_portal_pc?ac_id=1    从页面 CONFIG 里抠出真实 acid
    3. GET /cgi-bin/get_challenge     取一次性 token
    4. GET /cgi-bin/srun_portal       携带签名参数登录
    5. GET /cgi-bin/rad_user_dm       注销

.. warning::
   本适配器依据公开协议文档实现，作者未在真实深澜环境验证过。
   若你在使用中发现问题，欢迎提 Issue 附上 ``netkeeper doctor`` 的输出。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from typing import Any, Optional

from ..probe import PortalTarget
from ..transport import request
from .base import Adapter, LoginOutcome, SessionInfo, SessionStatus, explain

# ------------------------------------------------------------------ 编码
_DELTA = 0x9E3779B9
_MASK = 0xFFFFFFFF

# 深澜用一套置换过字母表的 Base64，直接 base64.b64encode 出来的结果服务端不认
_SRUN_ALPHA = "LVoJPiCN2R8G90yg+hmFHuacZ1OWMnrsSTXkYpUq/3dlbfKwv6xztjI7DeBE45QA"
_STD_ALPHA = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
_B64_TRANS = str.maketrans(_STD_ALPHA, _SRUN_ALPHA)


def _u32(value: int) -> int:
    return value & _MASK


def _to_words(data: bytes, include_length: bool) -> list[int]:
    """每 4 字节按小端打包成 uint32 数组。"""
    words = [
        int.from_bytes(data[i : i + 4].ljust(4, b"\x00"), "little")
        for i in range(0, len(data), 4)
    ]
    if include_length:
        words.append(len(data))
    return words


def xencode(data: bytes, key: bytes) -> bytes:
    """深澜自研的 XXTEA 变体加密。"""
    if not data:
        return b""
    v = _to_words(data, True)
    k = _to_words(key, False)
    while len(k) < 4:
        k.append(0)

    n = len(v) - 1
    z = v[n]
    total = 0
    rounds = 6 + 52 // (n + 1)

    while rounds > 0:
        rounds -= 1
        total = _u32(total + _DELTA)
        e = (total >> 2) & 3
        for p in range(n):
            y = v[p + 1]
            m = (z >> 5) ^ _u32(y << 2)
            m = _u32(m + (((y >> 3) ^ _u32(z << 4)) ^ (total ^ y)))
            m = _u32(m + (k[(p & 3) ^ e] ^ z))
            v[p] = z = _u32(v[p] + m)
        y = v[0]
        m = (z >> 5) ^ _u32(y << 2)
        m = _u32(m + (((y >> 3) ^ _u32(z << 4)) ^ (total ^ y)))
        m = _u32(m + (k[(n & 3) ^ e] ^ z))
        v[n] = z = _u32(v[n] + m)

    return b"".join(word.to_bytes(4, "little") for word in v)


def _custom_b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii").translate(_B64_TRANS)


def encode_user_info(info: dict[str, Any], token: str) -> str:
    """``{SRBX1}`` + 置换 Base64(xEncode(紧凑 JSON, token))。"""
    payload = json.dumps(info, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return "{SRBX1}" + _custom_b64(xencode(payload, token.encode("utf-8")))


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _hmac_md5(message: str, key: str) -> str:
    return hmac.new(key.encode("utf-8"), message.encode("utf-8"), hashlib.md5).hexdigest()


# ------------------------------------------------------------------ 适配器
class SrunAdapter(Adapter):
    name = "srun"
    label = "深澜 Srun（实验性）"
    needs_query = False
    supports_mac_auth = False

    # ------------------------------------------------------------ 识别
    @classmethod
    def detect(cls, base_url: str, timeout: float = 4.0) -> bool:
        host = (base_url or "").rstrip("/")
        for candidate in (host, host.replace("https://", "http://", 1)):
            url = f"{candidate}/cgi-bin/get_challenge?callback=jsonp&username=test&ip=0.0.0.0"
            try:
                resp = request(url, timeout=timeout, follow_redirects=False)
            except Exception:  # noqa: BLE001
                continue
            body = resp.json_like().lower()
            if "challenge" in body or '"error"' in body or "ecode" in body:
                return True
            if "srun" in resp.text.lower():
                return True
        return False

    # ------------------------------------------------------------ 底层
    def _local_ip(self) -> str:
        from ..netinfo import local_ip_for

        host = self.target.host or "127.0.0.1"
        return local_ip_for(host) or "127.0.0.1"

    def _jsonp(self, path: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        import urllib.parse

        query = dict(params or {})
        query.setdefault("callback", "jsonp")
        url = f"{self.target.base_url}{path}?{urllib.parse.urlencode(query)}"
        resp = request(
            url,
            timeout=self.timeout,
            follow_redirects=False,
            headers={"Referer": f"{self.target.base_url}/srun_portal_pc"},
        )
        text = resp.json_like()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return {"error": "bad_response", "raw": text[:200]}
        return data if isinstance(data, dict) else {"error": "bad_response"}

    def _real_acid(self) -> str:
        """页面里的 ac_id 只是主题编号，真正的 acid 藏在 CONFIG 里。"""
        override = self.options.get("srun_ac_id", "")
        if override:
            return override
        entry = self.options.get("srun_entry_ac_id", "1")
        resp = request(
            f"{self.target.base_url}/srun_portal_pc?ac_id={entry}&theme=proyx",
            timeout=self.timeout,
            follow_redirects=False,
        )
        match = re.search(r'acid\s*:\s*["\']?(\d+)', resp.text)
        if match:
            return match.group(1)
        return entry

    # ------------------------------------------------------------ 查询
    def session(self) -> SessionInfo:
        ip = self._local_ip()
        try:
            data = self._jsonp("/cgi-bin/rad_user_info", {"ip": ip})
        except Exception as exc:  # noqa: BLE001
            return SessionInfo(status=SessionStatus.UNKNOWN, detail=f"{type(exc).__name__}: {exc}")

        if str(data.get("error", "")).lower() == "ok":
            return SessionInfo(
                status=SessionStatus.ONLINE,
                user_id=str(data.get("user_name") or ""),
                user_index=f"{ip}_{data.get('user_name', '')}",
                mac="",
                data=data,
            )
        if not data:
            return SessionInfo(status=SessionStatus.UNKNOWN, detail="响应为空")
        return SessionInfo(
            status=SessionStatus.OFFLINE,
            data=data,
            detail=str(data.get("error") or data.get("ploy_msg") or ""),
        )

    # ------------------------------------------------------------ 登录
    def login(self, username: str, password: str, *, variant: str = "") -> LoginOutcome:
        ip = self._local_ip()
        try:
            acid = self._real_acid()
            challenge = self._jsonp(
                "/cgi-bin/get_challenge", {"username": username, "ip": ip}
            )
            token = str(challenge.get("challenge") or "")
            if not token:
                return LoginOutcome(
                    success=False,
                    message=str(challenge.get("error") or challenge.get("raw") or "未取得 challenge"),
                    hint=explain(str(challenge.get("error") or "")),
                )

            hmac_password = _hmac_md5(password, token)
            info = encode_user_info(
                {
                    "username": username,
                    "password": password,
                    "ip": ip,
                    "acid": str(acid),
                    "enc_ver": "srun_bx1",
                },
                token,
            )

            n = self.options.get("srun_n", "200")
            type_ = self.options.get("srun_type", "1")
            chksum = _sha1(
                token + username
                + token + hmac_password
                + token + str(acid)
                + token + ip
                + token + str(n)
                + token + str(type_)
                + token + info
            )

            data = self._jsonp(
                "/cgi-bin/srun_portal",
                {
                    "action": "login",
                    "username": username,
                    "password": "{MD5}" + hmac_password,
                    "os": "Windows 10",
                    "name": "windows",
                    "double_stack": "0",
                    "chksum": chksum,
                    "info": info,
                    "ac_id": str(acid),
                    "ip": ip,
                    "n": n,
                    "type": type_,
                },
            )
        except Exception as exc:  # noqa: BLE001
            return LoginOutcome(
                success=False,
                message=f"{type(exc).__name__}: {exc}",
                hint="深澜认证请求异常，请检查门户地址与网络连通性",
            )

        error = str(data.get("error", "")).lower()
        suc_msg = str(data.get("suc_msg", ""))
        if error == "ok":
            return LoginOutcome(
                success=True,
                message=str(data.get("ploy_msg") or suc_msg or "认证成功"),
                user_index=f"{ip}_{username}",
                raw=data,
            )
        message = str(data.get("ploy_msg") or suc_msg or error or "认证失败")
        return LoginOutcome(success=False, message=message, hint=explain(message), raw=data)

    # ------------------------------------------------------------ 保活
    def keepalive(self, user_index: str) -> tuple[bool, str]:
        """深澜没有独立的保活接口；查询在线状态本身即可刷新活动时间。"""
        info = self.session()
        if info.status is SessionStatus.ONLINE:
            return True, "在线"
        if info.status is SessionStatus.UNKNOWN:
            return True, f"状态未知（{info.detail}），本轮不判定为掉线"
        return False, "会话已不在线"

    # ------------------------------------------------------------ 下线
    def logout(self, user_index: str) -> tuple[bool, str]:
        ip = self._local_ip()
        username = self.options.get("username", "")
        if not username:
            # user_index 形如 "10.0.0.2_2023001"
            username = user_index.split("_", 1)[1] if "_" in user_index else ""
        ts = int(time.time())
        unbind = 1
        sign = _sha1(f"{ts}{username}{ip}{unbind}{ts}")
        try:
            data = self._jsonp(
                "/cgi-bin/rad_user_dm",
                {"ip": ip, "username": username, "time": ts, "unbind": unbind, "sign": sign},
            )
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"
        ok = str(data.get("error", "")).lower() == "ok"
        return ok, str(data.get("ploy_msg") or data.get("error") or "")
