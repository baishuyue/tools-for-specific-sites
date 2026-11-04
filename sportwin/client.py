# -*- coding: utf-8 -*-
"""运动世界校园 7.3.92 网关客户端（纯 Windows / 纯标准库）。

一次请求的组成：
    headers 明文（设备指纹 + timeStamp + nonce + uid + token）
    + tokenSign   = md5(sorted(timeStamp, token, uid) + 盐)
    + headerSign  = NetSec 信封(整个 headers 序列化后的 JSON)
    body          = NetSec 信封(业务 JSON)
服务端用 headerSign 里的 keyData 派生响应密钥，把业务数据加密回传。

所以请求能不能过，只取决于两件事：token 是否有效、签名是否正确。
两者都不需要 App 进程参与 —— 这就是去掉 MuMu 的技术前提。
"""
from __future__ import annotations

import json
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Callable, Optional

from . import protocol
from .protocol import Envelope, token_sign

HOST_RUN = "https://run.gxapp.iydsj.com"
HOST_DISCOVERY = "https://discovery.gxapp.iydsj.com"

# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------
EP_SERVER_TIME = HOST_RUN + "/api/v3/getserver/gmttime"
EP_USER_INFO = HOST_RUN + "/api/v70390/user/info"
EP_RUN_POLICY = HOST_RUN + "/api/v70103/runModePolicy"
EP_INDOOR_POLICY = HOST_RUN + "/api/v59/indoorRunModePolicy"
EP_GEO_FENCE = HOST_RUN + "/api/v1/getGeoFenceForRun"
EP_SEMESTER_INFO = HOST_RUN + "/api/v41/running/getPersonalSemesterInfo"
EP_SEMESTER_SUMMARY = HOST_RUN + "/api/v55/runnings/recordssummary/semester"
EP_RECORDS = HOST_RUN + "/api/v70230/runnings/records"
EP_ONE_RECORD = HOST_RUN + "/api/v70320/runnings/get_one_record"
EP_OBS_URL = HOST_RUN + "/api/obs/temporary/url"
EP_SAVE_RECORD = HOST_RUN + "/api/v70260/runnings/save/record"
EP_RUN_DETAIL_CONFIG = HOST_RUN + "/api/v70340/activity/rundetail/config"
EP_HOME_PAGE = HOST_RUN + "/api/v77/home/page"

#: 五点下发（计分跑必经打卡点），签名在 body 里
EP_POINTS = HOST_RUN + "/api/v560/get/1/distance/%d"

DEFAULT_DEVICE = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "appVersion": protocol.APP_VERSION,
    "physicPixel": "1080x2400",
    "logicPixel": "393x873",
    "isRoot": "0",
    "osType": "0",
    "cpuModel": "arm64-v8a",
    "deviceName": "Android Device",
    "osVersion": "13",
    "User-Agent": "okhttp/4.12.0",
}


def _load(data: Any) -> Any:
    """服务端有些接口把 data 再包一层 JSON 字符串，这里统一拆开。"""
    if isinstance(data, (bytes, bytearray)):
        data = data.decode("utf-8", "replace")
    if isinstance(data, str):
        text = data.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError:
            return data
    return data


def as_dict(data: Any, default: Optional[dict] = None) -> dict:
    data = _load(data)
    if isinstance(data, dict):
        return data
    return dict(default or {})


def as_list(data: Any) -> list:
    data = _load(data)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("records") or data.get("list") or []
    return []


class SportError(RuntimeError):
    """业务/网络错误统一封装，带服务端 error code。"""

    def __init__(self, message: str, code: Any = -1, raw: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.raw = raw


class SportClient:
    def __init__(
        self,
        uid: Any = None,
        token: Optional[str] = None,
        unid: Any = None,
        device: Optional[dict] = None,
        timeout: float = 15.0,
        retries: int = 3,
        proxy: Optional[str] = None,
        insecure: bool = False,
        log: Callable[[str], None] = lambda m: None,
    ) -> None:
        self.uid = str(uid) if uid not in (None, "", "-1", -1) else None
        self.token = str(token) if token else None
        self.unid = str(unid) if unid not in (None, "", "-1", -1) else None

        self.device = dict(DEFAULT_DEVICE)
        if device:
            for k, v in device.items():
                if v not in (None, ""):
                    self.device[k] = v
        self.device.setdefault("DeviceId", uuid.uuid4().hex)

        self.timeout = timeout
        self.retries = max(1, int(retries))
        self.log = log
        self._clock_offset_ms = 0

        ctx = ssl.create_default_context()
        if insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        self._ssl = ctx

        handlers = []
        if proxy:
            handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        else:
            handlers.append(urllib.request.ProxyHandler({}))
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
        self._opener = urllib.request.build_opener(*handlers)

    # -- 时间 --------------------------------------------------------------
    def now_ms(self) -> int:
        return int(time.time() * 1000) + self._clock_offset_ms

    def sync_time(self) -> int:
        """与服务端 GMT 对齐；跑前跑后各调一次，避免 stopTime 漂移。"""
        local0 = int(time.time() * 1000)
        data = self.get(EP_SERVER_TIME)
        try:
            server_ms = int(data if not isinstance(data, dict) else data.get("data", 0))
        except (TypeError, ValueError):
            server_ms = 0
        if server_ms > 1_000_000_000_000:
            rtt = int(time.time() * 1000) - local0
            self._clock_offset_ms = server_ms + rtt // 2 - int(time.time() * 1000)
        return self.now_ms()

    # -- 请求头 ------------------------------------------------------------
    def _build_headers(self, extra: Optional[dict] = None) -> dict:
        headers = dict(self.device)
        ts = str(self.now_ms())
        headers["timeStamp"] = ts
        headers["nonce"] = str(uuid.uuid4())
        headers["uid"] = self.uid or "-1"
        headers["token"] = self.token or ""
        headers["tokenSign"] = token_sign(
            {"timeStamp": ts, "token": headers["token"], "uid": headers["uid"]}
        )
        if extra:
            headers.update(extra)
        env = Envelope.wrap(json.dumps(headers, ensure_ascii=False, separators=(",", ":")))
        headers["headerSign"] = json.dumps(env.wire, separators=(",", ":"))
        return headers, env

    # -- 底层 --------------------------------------------------------------
    def _send(self, method: str, url: str, body: Any = None, extra_headers: Optional[dict] = None,
              timeout: Optional[float] = None) -> Any:
        last_err: Optional[Exception] = None
        for attempt in range(1, self.retries + 1):
            headers, env = self._build_headers(extra_headers)
            data = None
            if method.upper() != "GET" and body is not None:
                benv = Envelope.wrap(body)
                data = json.dumps(benv.wire, separators=(",", ":")).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
            try:
                with self._opener.open(req, timeout=timeout or self.timeout) as resp:
                    raw = resp.read().decode("utf-8", "replace")
                try:
                    obj = json.loads(raw)
                except ValueError:
                    raise SportError("网关返回非 JSON（可能被风控/WAF 拦截）：%s" % raw[:120], -2)
                return env.unwrap(obj)
            except urllib.error.HTTPError as exc:
                text = exc.read().decode("utf-8", "replace")
                last_err = SportError("HTTP %s: %s" % (exc.code, text[:160]), exc.code)
                if exc.code in (429, 500, 502, 503, 504) and attempt < self.retries:
                    time.sleep(min(2 ** attempt, 8))
                    continue
                raise last_err
            except (urllib.error.URLError, socket.timeout, ssl.SSLError, ConnectionError) as exc:
                last_err = SportError("网络异常：%s" % exc, -3)
                if attempt < self.retries:
                    time.sleep(min(2 ** attempt, 8))
                    continue
                raise last_err
        raise last_err or SportError("请求失败", -1)

    def _biz(self, method: str, url: str, body: Any = None, extra_headers: Optional[dict] = None,
             timeout: Optional[float] = None) -> Any:
        obj = self._send(method, url, body, extra_headers, timeout)
        if isinstance(obj, dict) and obj.get("error") not in (None, 10000):
            raise SportError(str(obj.get("message") or "业务失败"), obj.get("error"), obj)
        if isinstance(obj, dict) and "data" in obj:
            return obj["data"]
        return obj

    def get(self, url: str) -> Any:
        return self._biz("GET", url)

    def post(self, url: str, body: Any, timeout: Optional[float] = None) -> Any:
        return self._biz("POST", url, body, timeout=timeout)

    # -- 业务接口 ----------------------------------------------------------
    def user_info(self) -> dict:
        data = as_dict(self.get(EP_USER_INFO))
        if not isinstance(data, dict) or not data.get("uid"):
            raise SportError("未能读取账号信息，token 可能已失效（请在 App 内重新登录后重新导入 token）", -401)
        self.uid = str(data["uid"])
        if data.get("unid"):
            self.unid = str(data["unid"])
        return data

    def run_policy(self, run_mode: int = 1, unid: Any = None) -> dict:
        body = {
            "runMode": int(run_mode),
            "ruleUpdateTime": 0,
            "geoFenceUpdateTime": 0,
            "selectUnid": int(unid or self.unid or 0),
            "operateType": 0,
        }
        return as_dict(self.post(EP_INDOOR_POLICY if run_mode == 5 else EP_RUN_POLICY, body))

    def geo_fences(self) -> list:
        data = as_dict(self.post(EP_GEO_FENCE, {"geoFenceUpdateTime": 0}))
        return as_list(data.get("geoFences"))

    def semester_meta(self, run_mode: int = 1) -> dict:
        """学期元信息；即使用 sid=0 调用，服务端也会回真实 sid（如 9379）。"""
        return as_dict(self.post(EP_SEMESTER_INFO, {"runMode": int(run_mode), "sid": 0}))

    def semester_sid(self, run_mode: int = 1) -> int:
        try:
            return int(self.semester_meta(run_mode).get("sid") or 0)
        except Exception:
            return 0

    def semester_info(self, run_mode: int = 1) -> dict:
        return self.semester_meta(run_mode)

    def semester_summary(self, sid: Optional[int] = None) -> dict:
        """学期里程汇总；sid 传 0 服务端会返回全 0，必须先拿真实 sid。"""
        use = int(sid) if sid else self.semester_sid()
        data = as_dict(self.post(EP_SEMESTER_SUMMARY, {"sid": use}))
        data["_sid"] = use
        return data

    # -- 核对用 ---------------------------------------------------------
    def find_record(self, run_uuid: str, size: int = 10) -> dict:
        """按 uuid 在历史列表里找刚提交的记录。"""
        if not run_uuid:
            return {}
        for rec in self.records(page=1, size=size):
            if rec.get("uuid") == run_uuid:
                return rec
        return {}

    def today_records(self) -> list:
        from . import store          # 延迟导入，避免循环
        today0 = store.server_midnight_ms()
        return [r for r in self.records(page=1, size=10)
                if int(r.get("startTime") or 0) >= today0]

    def verify_submission(self, run_uuid: str = "") -> dict:
        """汇总"服务端到底收没收到、判没判有效"的全部证据。"""
        rec = self.find_record(run_uuid) if run_uuid else {}
        if not rec:
            todays = self.today_records()
            rec = todays[0] if todays else {}
        out: dict = {"record": rec, "detail": {}, "today": [], "semester": {}}
        if rec.get("rrid"):
            out["detail"] = as_dict(self.one_record(rec["rrid"]))
        out["today"] = self.today_records()
        out["semester"] = self.semester_summary()
        return out

    def records(self, page: int = 1, size: int = 10, sid: int = 0, sport_type: int = 1) -> list:
        data = self.post(EP_RECORDS, {"pageNum": int(page), "pageSize": int(size),
                                      "sid": int(sid), "type": int(sport_type)})
        return as_list(data)

    def one_record(self, rrid: Any) -> dict:
        return as_dict(self.post(EP_ONE_RECORD, {"rrid": int(rrid)}))

    def points(self, distance_m: int, lat: float, lon: float, run_area_id: int = -1,
               run_uuid: Optional[str] = None) -> dict:
        """计分跑的五个必经打卡点（服务端按围栏锚点随机下发）。"""
        url = EP_POINTS % int(distance_m)
        body = {
            "longitude": lon,
            "latitude": lat,
            "selectedUnid": int(self.unid or 0),
            "uuid": run_uuid or str(uuid.uuid4()),
            "runec": "",
            "runAreaId": int(run_area_id),
            "sign": protocol.url_sign(url),
        }
        return as_dict(self.post(url, body))

    def obs_url(self, object_key: str) -> dict:
        data = as_dict(self.post(EP_OBS_URL, {
            "bucketName": protocol.OBS_BUCKET,
            "objectKey": object_key,
            "method": "Put",
            "contentType": "text/plain",
        }))
        # 预签名头也可能是 JSON 字符串
        headers = _load(data.get("actualSignedRequestHeaders"))
        if isinstance(headers, dict):
            data["actualSignedRequestHeaders"] = headers
        for key in ("signedUrl", "url", "signedURL"):
            if isinstance(data.get(key), str):
                data[key] = data[key].strip()
        return data

    def save_record(self, body: dict) -> dict:
        # 结算接口服务端同步处理，耗时可能到几分钟，单独放宽超时
        raw = self.post(EP_SAVE_RECORD, body, timeout=300)
        val = _load(raw)
        if isinstance(val, int):
            return {"rrid": val}
        if isinstance(val, str) and val.strip().isdigit():
            return {"rrid": int(val.strip())}
        return as_dict(val)

    def run_detail_config(self, sport_type: int = 1) -> dict:
        return as_dict(self.get(EP_RUN_DETAIL_CONFIG + "?sportType=%d" % int(sport_type)))

    # -- 上传轨迹对象 ------------------------------------------------------
    def put_object(self, signed_url: str, payload: bytes, headers: Optional[dict] = None) -> int:
        req = urllib.request.Request(signed_url, data=payload, method="PUT",
                                     headers=headers or {"Content-Type": "text/plain"})
        try:
            with self._opener.open(req, timeout=max(60, self.timeout * 4)) as resp:
                return resp.status
        except urllib.error.HTTPError as exc:
            text = ""
            try:
                text = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            raise SportError("OBS 上传失败 HTTP %s %s" % (exc.code, text), exc.code)
        except (urllib.error.URLError, socket.timeout) as exc:
            raise SportError("OBS 上传网络异常：%s" % exc, -3)

    # -- 便捷 --------------------------------------------------------------
    def set_credentials(self, uid: Any = None, token: Optional[str] = None, unid: Any = None) -> None:
        if uid not in (None, "", "-1", -1):
            self.uid = str(uid)
        if token:
            self.token = str(token)
        if unid not in (None, "", "-1", -1):
            self.unid = str(unid)

    def probe(self) -> dict:
        """快速探活：拉一次账号信息，返回 uid/unid/姓名/学校。"""
        info = self.user_info()
        return {
            "uid": info.get("uid"),
            "unid": info.get("unid"),
            "name": info.get("name"),
            "username": info.get("username"),
            "campus": info.get("campusName") or info.get("universityName"),
            "class": info.get("gradeClass"),
        }


def quote(s: Any) -> str:
    return urllib.parse.quote(str(s))
