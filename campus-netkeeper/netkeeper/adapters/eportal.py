"""ePortal 族适配器。

覆盖国内高校最常见的一类 Web 认证接口 —— ``/eportal/InterFace.do``。
Dr.COM（城市热点）SAM+、锐捷、华为、H3C 的 Web 认证页面大多实现了
这套同名接口，请求/响应结构高度一致，差异集中在两点：

1. **编码层数**：用户名/密码/queryString 该做几次 ``encodeURIComponent``。
   Dr.COM 系做两次，部分定制页面只做一次。本适配器两种都试，成功的
   那一种写进状态文件缓存，之后不再试错。
2. **附加参数**：``service``、``passwordEncrypt``、``validcode`` 是否存在。

接口一览::

    GET  InterFace.do?method=getOnlineUserInfo   查询本机 IP 的会话
    POST InterFace.do?method=login               账号密码认证
    POST InterFace.do?method=keepalive           会话保活
    POST InterFace.do?method=registerMac         注册 MAC 无感认证
    POST InterFace.do?method=logout              主动下线
    GET  InterFace.do?method=getServices         可选服务列表
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from ..probe import PortalTarget
from ..transport import double_encode, request, uri_encode
from .base import (
    Adapter,
    LoginOutcome,
    SessionInfo,
    SessionStatus,
    explain,
    is_credential_error,
)

# 编码方式：名称 -> 编码函数
_VARIANTS = {
    "double": double_encode,
    "single": uri_encode,
}

_SCALAR_PAIR = re.compile(
    r'"([A-Za-z][A-Za-z0-9_]*)"\s*:\s*(?:"((?:[^"\\]|\\.)*)"|(null|true|false|-?\d+(?:\.\d+)?))'
)


def _salvage(text: str) -> dict[str, Any]:
    """从被截断或格式异常的响应里抢救顶层标量字段。"""
    data: dict[str, Any] = {}
    for key, str_value, scalar in _SCALAR_PAIR.findall(text):
        if key in data:  # 顶层字段先出现，嵌套对象中的同名字段不覆盖
            continue
        if str_value:
            data[key] = str_value
        elif scalar == "null":
            data[key] = None
        elif scalar == "true":
            data[key] = True
        elif scalar == "false":
            data[key] = False
        else:
            try:
                data[key] = int(scalar)
            except (TypeError, ValueError):
                data[key] = scalar
    return data


def parse_body(text: str) -> dict[str, Any]:
    """解析服务端返回的类 JSON 文本，容忍单引号与截断。"""
    text = (text or "").strip().lstrip("\ufeff")
    if not text:
        return {}
    for candidate in (text, text.replace("'", '"')):
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        return data if isinstance(data, dict) else {"raw": data}
    return _salvage(text)


class EportalAdapter(Adapter):
    name = "eportal"
    label = "ePortal 族（Dr.COM / 锐捷 / 华为 / H3C）"
    needs_query = True
    supports_mac_auth = True

    # ------------------------------------------------------------ 识别
    @classmethod
    def detect(cls, base_url: str, timeout: float = 4.0) -> bool:
        return cls.resolve_base(base_url, timeout) is not None

    @classmethod
    def resolve_base(cls, base_url: str, timeout: float = 4.0) -> Optional[str]:
        """确认接口根路径并返回它。

        识别信号用 ``getServices`` 的 JSON 响应 —— 这个接口在 ePortal 族里
        必然存在且返回结构化数据，比在首页 HTML 里找关键字可靠得多
        （很多门户的接口调用写在外部 JS 里，首页根本不含 ``InterFace.do``）。
        不同厂商的字段名不完全一致，因此按字段名集合判断。
        """
        markers = ("isService", "serviceContent", "defaultService", "userIndex", "result")
        for candidate in cls.candidate_bases(base_url):
            for method in ("getServices", "getOnlineUserInfo"):
                try:
                    resp = request(
                        f"{candidate}/InterFace.do?method={method}",
                        timeout=timeout,
                        follow_redirects=False,
                    )
                except Exception:  # noqa: BLE001
                    continue
                text = resp.text.strip().lstrip("\ufeff")
                if resp.status == 200 and text.startswith("{") and any(f'"{m}"' in text for m in markers):
                    return candidate
            # 兜底：首页是否引用了接口脚本
            page = cls._get_index(candidate, timeout)
            if page and "interface.do" in page.lower():
                return candidate
        return None

    @staticmethod
    def candidate_bases(base_url: str) -> list[str]:
        """给定门户地址，列出可能的接口根路径。"""
        base = (base_url or "").rstrip("/")
        if not base:
            return []
        candidates = [base]
        if not base.lower().rstrip("/").endswith("/eportal"):
            candidates.append(base + "/eportal")
        if base.startswith("https://"):
            candidates.append("http://" + base[len("https://") :])
        return candidates

    @staticmethod
    def _get_index(base: str, timeout: float) -> str:
        try:
            resp = request(base + "/index.jsp", timeout=timeout, follow_redirects=False)
        except Exception:  # noqa: BLE001
            return ""
        if resp.status != 200:
            return ""
        return resp.text

    # ------------------------------------------------------------ 底层
    def _endpoint(self, method: str) -> str:
        return f"{self.target.base_url}/InterFace.do?method={method}"

    def _call(
        self,
        method: str,
        *,
        http_method: str = "POST",
        data: Optional[str] = None,
    ) -> dict[str, Any]:
        resp = request(
            self._endpoint(method),
            method=http_method,
            data=data,
            timeout=self.timeout,
            follow_redirects=False,
            headers={"Referer": f"{self.target.base_url}/index.jsp"},
        )
        return parse_body(resp.text)

    # ------------------------------------------------------------ 查询
    def session(self, retries: int = 1) -> SessionInfo:
        last_error = ""
        for attempt in range(retries + 1):
            try:
                data = self._call("getOnlineUserInfo", http_method="GET")
            except Exception as exc:  # noqa: BLE001
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < retries:
                    import time

                    time.sleep(0.8)
                    continue
                return SessionInfo(status=SessionStatus.UNKNOWN, detail=last_error)

            # 判定依据是 userId 是否存在，而不是 result 的取值。
            # 实测某些门户在会话刚建立时会返回 result="wait"（用户信息不全），
            # 但 userId 已带上；真正离线时 userId 一定为空。
            user_id = str(data.get("userId") or data.get("userName") or "")
            online = str(data.get("onlineUser") or "")
            if user_id or online:
                return SessionInfo(
                    status=SessionStatus.ONLINE,
                    user_id=user_id or online,
                    user_index=str(data.get("userIndex") or ""),
                    mac=str(data.get("userMac") or ""),
                    data=data,
                    detail=str(data.get("result") or ""),
                )

            if not data:
                return SessionInfo(status=SessionStatus.UNKNOWN, detail="响应为空")

            return SessionInfo(
                status=SessionStatus.OFFLINE,
                data=data,
                detail=str(data.get("message") or data.get("result") or ""),
            )

        return SessionInfo(status=SessionStatus.UNKNOWN, detail=last_error)

    # ------------------------------------------------------------ 登录
    def login(self, username: str, password: str, *, variant: str = "") -> LoginOutcome:
        variants = [variant] if variant in _VARIANTS else list(_VARIANTS)
        last: Optional[LoginOutcome] = None

        for name in variants:
            encode = _VARIANTS[name]
            outcome = self._login_once(encode, username, password, name)
            if outcome.success:
                return outcome
            last = outcome
            # 只有"疑似编码问题"才换下一种编码；
            # 若服务端明确报欠费/停用/设备数超限，换编码没有意义。
            if not is_credential_error(outcome.message):
                return outcome

        return last or LoginOutcome(success=False, message="登录未执行")

    def _login_once(self, encode, username: str, password: str, variant: str) -> LoginOutcome:
        body = (
            f"userId={encode(username)}"
            f"&password={encode(password)}"
            f"&service={encode(self.options.get('service', ''))}"
            f"&queryString={encode(self.target.raw_query)}"
            f"&operatorPwd={encode('')}"
            f"&operatorUserId={encode('')}"
            f"&validcode={encode('')}"
            f"&passwordEncrypt={encode('false')}"
        )
        data = self._call("login", http_method="POST", data=body)
        result = str(data.get("result", "")).lower()
        message = str(data.get("message", "") or "")

        if result in ("success", "ok"):
            return LoginOutcome(
                success=True,
                message=message or "认证成功",
                user_index=str(data.get("userIndex") or ""),
                variant=variant,
                raw=data,
            )

        return LoginOutcome(
            success=False,
            message=message or result or "认证失败（服务端未返回原因）",
            hint=explain(message),
            variant=variant,
            raw=data,
        )

    # ------------------------------------------------------------ 保活
    def keepalive(self, user_index: str) -> tuple[bool, str]:
        if not user_index:
            return False, "缺少 userIndex"
        data = self._call("keepalive", data=f"userIndex={user_index}")
        result = str(data.get("result", "")).lower()
        message = str(data.get("message", "") or "")
        return result in ("success", "ok"), message or result

    # ------------------------------------------------------------ 无感认证
    def register_mac(self, mac: str, user_index: str) -> tuple[bool, str]:
        if not mac or not user_index:
            return False, "缺少 mac 或 userIndex"
        data = self._call("registerMac", data=f"mac={mac}&userIndex={user_index}")
        result = str(data.get("result", "")).lower()
        message = str(data.get("message", "") or "")

        if result in ("success", "ok"):
            return True, message or "注册成功"
        # 该接口不幂等：重复注册会报"MAC地址已经被注册"，语义上等价于已注册
        if any(hint in message for hint in ("已经被注册", "已被注册", "已经注册", "已注册", "重复注册")):
            return True, f"该 MAC 此前已完成注册（{message}）"
        return False, message or result

    # ------------------------------------------------------------ 下线
    def logout(self, user_index: str) -> tuple[bool, str]:
        if not user_index:
            return False, "缺少 userIndex"
        data = self._call("logout", data=f"userIndex={user_index}")
        result = str(data.get("result", "")).lower()
        message = str(data.get("message", "") or "")
        return result in ("success", "ok"), message or result

    # ------------------------------------------------------------ 诊断
    def describe(self) -> dict[str, str]:
        info = super().describe()
        try:
            page = self._get_index(self.target.base_url, self.timeout)
        except Exception:  # noqa: BLE001
            page = ""
        if page:
            info["页面编码"] = "GBK/GB18030" if "charset=gbk" in page.lower() else "UTF-8"
            flag = re.search(r"passwordEncrypt\s*[:=]\s*[\"']?(true|false)", page, re.I)
            if flag:
                info["密码加密"] = flag.group(1).lower()
        return info
