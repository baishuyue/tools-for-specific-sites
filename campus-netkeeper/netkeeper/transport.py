"""底层 HTTP 传输：强制绕过代理 + TLS 安全级别自动降级。

为什么必须绕过代理
------------------
校园网认证请求必须由接入控制器（AC）直接看到本机源地址。若走系统代理
（Clash / v2ray 等），AC 看到的是代理地址，认证必然失败；反过来，认证
流量绕过代理也保证客户端软件不会被代理规则干扰。

为什么需要 TLS 降级
-------------------
国内高校的认证设备（Dr.COM AC / SAM+、深澜、锐捷）普遍是若干年前的
国产设备，其 DH 密钥长度与证书算法低于 OpenSSL 3.x 的默认安全级别
（SECLEVEL=2），直接用默认配置会得到 ``SSLV3_ALERT_HANDSHAKE_FAILURE``
或 ``DH_KEY_TOO_SMALL``。这里按"尽量安全"的顺序逐级降级，握手成功后
固定复用该级别。
"""

from __future__ import annotations

import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import NamedTuple, Optional

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 单次响应体读取上限。门户的 getOnlineUserInfo 会返回内嵌 base64 公告图的
# 大 JSON（实测可达 276KB），上限过紧会把关键字段截断导致解析失败。
MAX_BODY = 1024 * 1024


class HttpResponse(NamedTuple):
    status: int
    headers: dict[str, str]
    body: bytes
    url: str

    @property
    def text(self) -> str:
        return decode_bytes(self.body)

    @property
    def location(self) -> str:
        return self.headers.get("location", "") or ""

    def json_like(self) -> str:
        """返回去掉 JSONP 外壳 / BOM 之后的文本。"""
        text = self.text.strip().lstrip("\ufeff")
        # 形如 cb({...}) 或 jsonp({...})
        start = text.find("(")
        end = text.rfind(")")
        if 0 < start < end and text[:start].strip().isidentifier():
            return text[start + 1 : end].strip()
        return text


# --------------------------------------------------------------------- 编码
def uri_encode(value: str) -> str:
    """等价于 JavaScript 的 ``encodeURIComponent``。"""
    return urllib.parse.quote(value, safe="-_.!~*'()")


def double_encode(value: str) -> str:
    """复刻 ``encodeURIComponent(encodeURIComponent(x))``。"""
    return uri_encode(uri_encode(value))


def decode_bytes(raw: bytes) -> str:
    """尽力解码响应体：JSON 接口多为 UTF-8，老门户页面多为 GBK。"""
    if not raw:
        return ""
    for encoding in ("utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def sniff_charset(headers: dict[str, str], body: bytes) -> str:
    """从 Content-Type 或 HTML meta 推断字符集。"""
    ctype = (headers.get("content-type") or "").lower()
    if "charset=" in ctype:
        return ctype.split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
    head = body[:2048].lower()
    marker = b"charset="
    pos = head.find(marker)
    if pos != -1:
        frag = head[pos + len(marker) : pos + len(marker) + 24]
        name = frag.split(b'"')[0].split(b"'")[0].split(b">")[0].split(b";")[0]
        try:
            return name.decode("ascii").strip() or "utf-8"
        except UnicodeDecodeError:
            return "utf-8"
    return "utf-8"


# --------------------------------------------------------------------- TLS
# (是否校验证书, OpenSSL 安全级别)
_TLS_PROFILES: tuple[tuple[bool, int], ...] = (
    (True, 2),   # 现代证书：完全校验
    (True, 1),   # 老设备的 DH 参数不满足 SECLEVEL=2
    (True, 0),   # 更老：短密钥 / MD5 签名证书
    (False, 0),  # 自签名或中间人代理：最终兜底
)
_state = {"index": 0}
_contexts: dict[int, ssl.SSLContext] = {}


def _context_for(index: int) -> ssl.SSLContext:
    cached = _contexts.get(index)
    if cached is not None:
        return cached

    verify, level = _TLS_PROFILES[index]
    context = ssl.create_default_context()
    if not verify:
        # 顺序很关键：必须先关 check_hostname 才能把 verify_mode 设为 CERT_NONE
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    if level != 2:
        for spec in (f"DEFAULT@SECLEVEL={level}", f"ALL:@SECLEVEL={level}"):
            try:
                context.set_ciphers(spec)
                break
            except ssl.SSLError:
                continue
    _contexts[index] = context
    return context


def _is_tls_error(exc: BaseException) -> bool:
    if isinstance(exc, ssl.SSLError):
        return True
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, ssl.SSLError):
            return True
        text = f"{reason}".lower()
        return any(k in text for k in ("ssl", "tls", "handshake", "dh key", "certificate"))
    return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, hdrs, newurl):  # noqa: D102
        return None


def _build_opener(profile: int, follow: bool) -> urllib.request.OpenerDirector:
    handlers: list = [
        # 空 ProxyHandler = 彻底禁用代理
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=_context_for(profile)),
    ]
    if not follow:
        handlers.insert(0, _NoRedirect())
    return urllib.request.build_opener(*handlers)


def _send(
    url: str,
    method: str,
    data: Optional[str],
    timeout: float,
    follow: bool,
    headers: Optional[dict[str, str]],
    profile: int,
) -> HttpResponse:
    payload = data.encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=payload, method=method)
    req.add_header("User-Agent", DEFAULT_UA)
    req.add_header("Accept", "*/*")
    req.add_header("Accept-Language", "zh-CN,zh;q=0.9")
    if payload is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8")
    for key, val in (headers or {}).items():
        req.add_header(key, val)

    opener = _build_opener(profile, follow)
    try:
        with opener.open(req, timeout=timeout) as resp:
            return HttpResponse(
                status=resp.status,
                headers={k.lower(): v for k, v in resp.headers.items()},
                body=resp.read(MAX_BODY),
                url=resp.geturl(),
            )
    except urllib.error.HTTPError as exc:
        # 3xx / 4xx / 5xx 在此返回；302 的 Location 正是登录参数的来源
        try:
            body = exc.read(MAX_BODY)
        except Exception:  # noqa: BLE001
            body = b""
        return HttpResponse(
            status=exc.code,
            headers={k.lower(): v for k, v in (exc.headers or {}).items()},
            body=body,
            url=url,
        )


def request(
    url: str,
    *,
    method: str = "GET",
    data: Optional[str] = None,
    timeout: float = 8.0,
    follow_redirects: bool = False,
    headers: Optional[dict[str, str]] = None,
) -> HttpResponse:
    """发起一次 HTTP(S) 请求，自动处理 TLS 兼容性降级。

    默认**不跟随**重定向：被困在认证页时，重定向 Location 里正好携带了
    登录所需的全部参数，必须截获它。
    """
    last_error: Optional[BaseException] = None
    for index in range(_state["index"], len(_TLS_PROFILES)):
        try:
            response = _send(url, method, data, timeout, follow_redirects, headers, index)
        except Exception as exc:  # noqa: BLE001
            if _is_tls_error(exc):
                last_error = exc
                continue
            raise
        # 握手成功则固定使用该级别，后续请求不再重复试错
        _state["index"] = index
        return response

    if last_error is not None:
        raise last_error
    raise RuntimeError("请求未能完成")


def tls_profile_name() -> str:
    """当前生效的 TLS 兼容级别（用于诊断输出）。"""
    verify, level = _TLS_PROFILES[_state["index"]]
    return f"{'校验证书' if verify else '不校验证书'}/SECLEVEL={level}"


# --------------------------------------------------------------------- 网络
def tcp_open(host: str, port: int = 443, timeout: float = 4.0) -> bool:
    """目标主机端口是否可建立 TCP 连接。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def outbound_ip(host: str, port: int = 443, timeout: float = 4.0) -> Optional[str]:
    """获取"访问该主机时使用的本机源 IP"。

    比枚举网卡更准确：多网卡（含虚拟机虚拟网卡）环境下，它返回真正
    走默认路由出去的那个地址。
    """
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            return sock.getsockname()[0]
    except OSError:
        return None


def split_url(url: str) -> tuple[str, str, str]:
    """拆成 (scheme, host, path)，缺失协议时按 https 处理。"""
    text = (url or "").strip()
    if not text:
        return "", "", ""
    if "://" not in text:
        text = "https://" + text
    parsed = urllib.parse.urlsplit(text)
    path = parsed.path.rstrip("/")
    return parsed.scheme or "https", parsed.hostname or "", path
