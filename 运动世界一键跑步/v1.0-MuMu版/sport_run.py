# -*- coding: utf-8 -*-
"""
sport_run.py —— 运动世界校园「一键跑步」Windows 工具（GUI + CLI）

设计目标：
  * 在 Windows 上双击即用，一键完成当天校园跑（约 2km / 5 打卡点）。
  * 自动后台拉起并连接本机 MuMu 模拟器里的「运动世界校园」，全程无需手动操作。
  * 轨迹生成、加密、对象存储上传、签名、提交、核验全部在本机 Python 完成；
    请求签名由安卓端签名服务（SportSniffer 模块，127.0.0.1:8765，经 adb forward）
    提供，PC 端仅调用本地接口。

为什么需要保留一个安卓环境：
  1) 客户端请求头依赖 App 内安全组件生成的签名，PC 端无法独立生成；
     缺少有效签名时网关直接返回空响应，无法完成任何业务请求。
  2) 该 App 未提供账号密码登录（仅手机号一键登录 / 短信验证码）。
  因此保留一个已登录的 App（MuMu 后台静默运行）作为本地签名服务，是本工具
  当前唯一稳定、可长期维护的方案。首次按 README 部署一次，之后每天双击即可。

用法：
  双击 “一键跑步.bat”  或  python sport_run.py            打开图形界面
  python sport_run.py run            命令行直接跑一次
  python sport_run.py status         查看环境 / 账号 / 今日额度
  python sport_run.py doctor         环境自检（MuMu / adb / 依赖 / 数据）
"""
from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import json
import math
import os
import random
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

# build: 626169736875797565-2026
__build_ref__ = "YmFpc2h1eXll"

# ----------------------------------------------------------------------
# 路径与常量
# ----------------------------------------------------------------------
APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
CONFIG_PATH = APP_DIR / "config.json"
PKG = "com.zjwh.android_wh_physicalfitness"
RPC_PORT = 8765

# 轨迹加密参数（AES/ECB/PKCS5）
TRACK_KEY = bytes.fromhex("736d6b6c646f7370646f736c64616161")
# 成绩签名盐
SIGN_SALT = "2slhe02lsfiwowlcixisla_sls-_slaor"
# 提交报文字段顺序（服务端要求固定顺序）
CORE_FIELDS = [
    "address", "avgPower", "avgStepFreq", "calorie", "complete", "errorCode",
    "faceCheck", "geeToken", "getPrize", "goalId", "policy", "selDistance",
    "selectedUnid", "selRunTime", "speed", "sportType", "startTime", "status",
    "stopTime", "themeId", "totalAscent", "totalDis", "totalSteps", "totalTime",
    "uid", "unauthorized", "unCompleteReason", "useMobilityTools", "uuid",
    "validDis", "validTime",
]
COS_BUCKET = "iydsj-hbase-hot"
HOST = "https://run.gxapp.iydsj.com"

DEFAULT_CONFIG = {
    "mumu_nx_dir": r"D:\Program Files\Netease\MuMu\nx_main",
    "device": "127.0.0.1:16384",
    "vmindex": "0",
    "hide_window_after_launch": True,
    "boot_timeout_sec": 180,
    "semester_id": 9379,
}


# ----------------------------------------------------------------------
# 通用工具
# ----------------------------------------------------------------------
def _utf8_stream() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


class ToolError(RuntimeError):
    pass


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


# ----------------------------------------------------------------------
# 环境管理：MuMu / adb / App / RPC
# ----------------------------------------------------------------------
class EnvManager:
    def __init__(self, cfg: dict, log: Callable[[str], None] = print):
        self.cfg = cfg
        self.log = log
        self.adb_path = str(Path(cfg["mumu_nx_dir"]) / "adb.exe")
        self.mgr_path = str(Path(cfg["mumu_nx_dir"]) / "MuMuManager.exe")
        self.device = cfg.get("device") or ""
        self._fwd = False

    # ---- 子进程 ----
    def _run(self, argv: list[str], timeout: int = 40) -> tuple[int, str, str]:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
        return p.returncode, p.stdout or "", p.stderr or ""

    def adb(self, args: list[str], timeout: int = 40) -> tuple[int, str, str]:
        argv = [self.adb_path]
        if self.device:
            argv += ["-s", self.device]
        argv += args
        return self._run(argv, timeout=timeout)

    def mumu(self, args: list[str], timeout: int = 60) -> tuple[int, str, str]:
        return self._run([self.mgr_path] + args, timeout=timeout)

    def adb_connect(self) -> None:
        """MuMu 网络 adb 端口需显式 connect；adb server 重启/换会话后会丢失。"""
        if self.device and ":" in self.device:
            try:
                self._run([self.adb_path, "connect", self.device], timeout=10)
            except Exception:
                pass

    # ---- 探测 ----
    def device_online(self) -> bool:
        try:
            self.adb_connect()
            rc, out, _ = self._run([self.adb_path, "devices"])
            if rc != 0:
                return False
            if self.device:
                return any(self.device in line and "\tdevice" in line
                           for line in out.splitlines())
            return "\tdevice" in out
        except Exception:
            return False

    def _info(self) -> dict | None:
        try:
            rc, out, _ = self.mumu(["info", "-v", str(self.cfg["vmindex"])], timeout=30)
            if rc == 0:
                return json.loads(out)
        except Exception:
            return None
        return None

    def player_running(self) -> bool:
        info = self._info()
        return bool(info and info.get("is_android_started"))

    # ---- 启动 ----
    def launch_player(self) -> None:
        if self.player_running():
            self.log("MuMu 实例已在运行。")
            return
        self.log("正在后台启动 MuMu 实例（index=%s）..." % self.cfg["vmindex"])
        rc, out, err = self.mumu(["control", "-v", str(self.cfg["vmindex"]), "launch"], timeout=60)
        if rc != 0:
            raise ToolError("MuMu 启动失败：%s %s" % (out, err))

    def wait_boot(self, timeout: int | None = None) -> None:
        timeout = timeout or int(self.cfg.get("boot_timeout_sec", 180))
        self.log("等待安卓系统启动 ...")
        end = time.time() + timeout
        while time.time() < end:
            self.adb_connect()
            if self.device_online():
                rc, out, _ = self.adb(["shell", "getprop", "sys.boot_completed"], timeout=15)
                if out.strip() == "1":
                    self.log("安卓系统已就绪。")
                    return
            time.sleep(3)
        raise ToolError("等待安卓启动超时；请手动打开 MuMu 后重试。")

    def ensure_forward(self) -> None:
        if self._fwd:
            return
        rc, out, err = self.adb(["forward", "tcp:%d" % RPC_PORT, "tcp:%d" % RPC_PORT])
        if rc != 0:
            raise ToolError("adb forward 失败：%s %s" % (out, err))
        self._fwd = True

    def start_app(self) -> None:
        self.log("启动运动世界校园 ...")
        self.adb(["shell", "am", "force-stop", PKG], timeout=20)
        time.sleep(2)
        self.ensure_forward()
        self.adb(["shell", "monkey", "-p", PKG, "-c",
                  "android.intent.category.LAUNCHER", "1"], timeout=20)

    def maybe_hide(self) -> None:
        if self.cfg.get("hide_window_after_launch"):
            try:
                self.mumu(["control", "-v", str(self.cfg["vmindex"]), "hide_window"], timeout=20)
                self.log("MuMu 已切换到后台静默运行。")
            except Exception:
                pass

    def _http(self, path: str, payload: dict | None = None, timeout: int = 30) -> dict:
        import urllib.request
        url = "http://127.0.0.1:%d%s" % (RPC_PORT, path)
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST" if payload is not None else "GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def health(self) -> dict:
        self.ensure_forward()
        return self._http("/health", None, timeout=8)

    def wait_rpc_ready(self, timeout: int = 90) -> None:
        self.log("等待 App 内签名服务就绪 ...")
        end = time.time() + timeout
        last = ""
        while time.time() < end:
            try:
                h = self.health()
                if h.get("good") or h.get("last"):
                    self.log("签名服务已就绪。")
                    return
                last = json.dumps(h, ensure_ascii=False)[:120]
            except Exception as e:
                last = str(e)[:120]
            time.sleep(2)
        raise ToolError("App 签名服务未就绪（%s）。请确认运动世界已登录、SportSniffer 已启用。" % last)

    def call(self, method: str, url: str, body: Any = None, timeout: int = 60) -> tuple[int, Any]:
        self.ensure_forward()
        payload: dict = {"method": method.upper(), "url": url}
        if body is not None:
            payload["body"] = body if isinstance(body, str) else json.dumps(
                body, ensure_ascii=False, separators=(",", ":"))
        obj = self._http("/call", payload, timeout=timeout)
        if not obj.get("ok"):
            raise ToolError("App 代发失败：%s" % obj.get("error"))
        return obj.get("code"), obj.get("data")

    def biz(self, method: str, path: str, body: Any = None, timeout: int = 60) -> dict:
        url = path if path.startswith("http") else HOST + path
        _, data = self.call(method, url, body, timeout=timeout)
        if isinstance(data, dict) and data.get("error") not in (None, 10000):
            raise ToolError("业务错误 error=%s message=%s" % (data.get("error"), data.get("message")))
        return data

    def biz_data(self, method: str, path: str, body: Any = None, timeout: int = 60) -> Any:
        d = self.biz(method, path, body, timeout=timeout)
        return d.get("data") if isinstance(d, dict) else d

    # ---- 一键编排 ----
    def prepare(self) -> None:
        """确保 MuMu + App + 签名服务全部就绪（后台静默）。"""
        if not Path(self.adb_path).exists():
            raise ToolError("找不到 adb：%s，请在 config.json 设置 mumu_nx_dir" % self.adb_path)
        if not self.device_online():
            self.launch_player()
            self.wait_boot()
        else:
            self.log("MuMu / adb 已连接。" if self.device else "adb 已连接。")
        self.ensure_forward()
        # 若 App 已就绪则不必重启
        try:
            h = self.health()
            if not (h.get("good") or h.get("last")):
                raise RuntimeError("not ready")
        except Exception:
            self.start_app()
            self.wait_rpc_ready()
        self.maybe_hide()

    def whoami(self) -> dict:
        d = self.biz_data("GET", "/api/v70390/user/info")
        if not isinstance(d, dict) or not d.get("uid"):
            raise ToolError("未能读取账号信息，运动世界可能未登录，请在 App 内登录一次。")
        return d


# ----------------------------------------------------------------------
# 密码学（纯 Python 可复现部分）
# ----------------------------------------------------------------------
def make_signature(original_sign: str) -> str:
    """按固定字段顺序重排，末尾追加盐后计算 MD5。"""
    pairs: dict[str, str] = {}
    for part in original_sign.replace("\\u0026", "&").split("&"):
        k, v = part.split("=", 1)
        pairs[k] = v
    s = "&".join(k + "=" + pairs[k] for k in CORE_FIELDS if k in pairs)
    return hashlib.md5((s + SIGN_SALT).encode("utf-8")).hexdigest()


def encrypt_track(plain_obj: dict) -> bytes:
    try:
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import pad
    except Exception:
        raise ToolError("缺少 pycryptodome，请运行：pip install pycryptodome")
    raw = json.dumps(plain_obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    enc = AES.new(TRACK_KEY, AES.MODE_ECB).encrypt(pad(raw, 16))
    return base64.encodebytes(enc)  # MIME 带换行，与 App 一致


# ----------------------------------------------------------------------
# 跑步核心
# ----------------------------------------------------------------------
def _meters(lat1, lng1, lat2, lng2):
    return math.sqrt(((lat1 - lat2) * 111000.0) ** 2 + ((lng1 - lng2) * 97000.0) ** 2)


def run_once(env: EnvManager, log: Callable[[str], None] = print, dry: bool = False) -> dict:
    from Crypto.Cipher import AES  # noqa: F401 提前触发依赖检查
    sid = env.cfg.get("semester_id", 9379)

    def _summary():
        s = env.biz_data("POST", "/api/v55/runnings/recordssummary/semester",
                         {"sid": sid}, timeout=30)
        return json.loads(s) if isinstance(s, str) else s

    def _records():
        dd = env.biz_data("POST", "/api/v70230/runnings/records",
                          {"pageNum": 1, "pageSize": 10, "sid": sid, "type": 1}, timeout=30)
        if isinstance(dd, str):
            dd = json.loads(dd)
        return dd if isinstance(dd, list) else dd.get("records", [])

    # 预检：今天是否已完成，或最近一次提交是否仍在核验（重复点击秒级返回，避免重复提交）
    _today0 = datetime.datetime.now().replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000
    _today_recs = [r for r in _records() if r.get("startTime", 0) >= _today0]
    for _r in _today_recs:
        if _r.get("complete") and _r.get("validDis", 0) >= 1000:
            _x = env.biz_data("POST", "/api/v70320/runnings/get_one_record",
                              {"uuid": _r["uuid"], "rrid": _r["rrid"], "calculateBadge": False}, timeout=60)
            _x = json.loads(_x) if isinstance(_x, str) else _x
            log("今天已完成跑步（rrid=%s，%.2fkm），无需重复提交。%s" % (
                _r["rrid"], _x.get("validDis", 0) / 1000.0, _x.get("statusInfo", "")))
            return {"uuid": _r["uuid"], "rrid": _r["rrid"],
                    "success": True, "already": True, "complete": True,
                    "validDis": _x.get("validDis")}

    # 最近一次提交（last_submit.json）若在 20 分钟内、属于今天，则仍在核验，不重复提交
    _last = APP_DIR / "last_submit.json"
    if _last.exists():
        try:
            _lj = json.loads(_last.read_text(encoding="utf-8"))
            _lbody = _lj.get("body", {})
            _lstop = _lbody.get("stopTime", 0)
            if _lstop >= _today0 and (time.time() * 1000 - _lstop) < 20 * 60 * 1000:
                _luuid = _lj.get("uuid")
                _hit = [r for r in _today_recs if r.get("uuid") == _luuid]
                if _hit and _hit[0].get("complete"):
                    log("最近一次提交已成功（rrid=%s，%.2fkm）。" %
                        (_hit[0]["rrid"], _hit[0].get("validDis", 0) / 1000.0))
                    return {"uuid": _luuid, "success": True, "already": True,
                            "validDis": _hit[0].get("validDis")}
                log("最近一次提交（uuid=%s）仍在服务端核验中（通常 5–12 分钟），请稍后点“刷新状态”查看，不要重复提交。" % _luuid)
                return {"uuid": _luuid, "success": False, "pending": True, "recheck": True}
        except Exception:
            pass

    tpl_track = json.loads((DATA_DIR / "sample_trajectory_plain.json").read_text(encoding="utf-8"))
    pts = json.loads(tpl_track["allLocJson"])
    tpl_body = json.loads((DATA_DIR / "save_2264_body.json").read_text(encoding="utf-8"))

    # 1) 时间平移：stopTime=现在，startTime=现在-模板总时长
    old_start = tpl_body["startTime"]
    old_stop = tpl_body["stopTime"]
    dur = old_stop - old_start
    now_ms = int(time.time() * 1000)
    new_stop = now_ms
    new_start = now_ms - dur
    delta = new_start - old_start

    def parse_dt(s):
        return datetime.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")

    for p in pts:
        t = parse_dt(p["gainTime"])
        ms = int(t.timestamp() * 1000) + delta
        p["gainTime"] = datetime.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
        p["flag"] = p["flag"] + delta

    # 2) GPS 噪声：普通点 σ=1.6m，打卡点 22m 内 σ=0.5m，保证打卡且每日不雷同
    fp0 = json.loads(tpl_body["fivePointJson"])
    checkpoints = json.loads(fp0["fivePointJson"])
    rng = random.Random(int(time.time()))
    for p in pts:
        near = min(_meters(p["gLat"], p["gLng"], c["glat"], c["glon"]) for c in checkpoints)
        sigma = 0.5 if near < 22 else 1.6
        p["gLat"] = round(p["gLat"] + rng.gauss(0, sigma) / 111000.0, 8)
        p["gLng"] = round(p["gLng"] + rng.gauss(0, sigma) / 97000.0, 8)
        p["radius"] = round(2.2 + rng.random() * 0.25, 6)

    new_uuid = str(uuid.uuid4())
    hourdir = datetime.datetime.fromtimestamp(new_start / 1000).strftime("%Y%m%d%H")
    object_key = "run_data/%s/%s.json" % (hourdir, new_uuid)

    cipher = encrypt_track({
        "allLocJson": json.dumps(pts, ensure_ascii=False, separators=(",", ":")),
        "useZip": False,
    })
    log("轨迹点数 %d，密文 %d bytes；目标里程 %.2f km" %
        (len(pts), len(cipher), tpl_body["totalDis"] / 1000.0))

    # 3) 构造 save body（关键：uid 必须取当前登录账号，切换账号后模板里的 uid 不匹配会被拒）
    me = env.biz_data("GET", "/api/v70390/user/info", timeout=30)
    current_uid = me["uid"]
    body = json.loads(json.dumps(tpl_body))
    body["uuid"] = new_uuid
    body["uid"] = current_uid
    body["startTime"] = new_start
    body["stopTime"] = new_stop
    fp = json.loads(body["fivePointJson"])
    inner = json.loads(fp["fivePointJson"])
    for q in inner:
        q["flag"] = q["flag"] + delta
    fp["fivePointJson"] = json.dumps(inner, ensure_ascii=False, separators=(",", ":"))
    body["fivePointJson"] = json.dumps(fp, ensure_ascii=False, separators=(",", ":"))

    op: dict[str, str] = {}
    for pair in tpl_body["originalSign"].replace("\\u0026", "&").split("&"):
        k, v = pair.split("=", 1)
        op[k] = v
    op["uuid"] = new_uuid
    op["uid"] = str(current_uid)
    op["startTime"] = str(new_start)
    op["stopTime"] = str(new_stop)
    new_orig = "&".join(k + "=" + op[k] for k in sorted(op.keys()))
    body["originalSign"] = new_orig
    body["signature"] = make_signature(new_orig)
    log("新 uuid：%s" % new_uuid)
    log("开始 %s — 结束 %s（用时 %d 秒）" % (
        datetime.datetime.fromtimestamp(new_start / 1000).strftime("%H:%M:%S"),
        datetime.datetime.fromtimestamp(new_stop / 1000).strftime("%H:%M:%S"),
        body["totalTime"]))

    if dry:
        (APP_DIR / "dryrun.json").write_text(
            json.dumps({"objectKey": object_key, "body": body}, ensure_ascii=False, indent=1),
            encoding="utf-8")
        (APP_DIR / "dryrun_cipher.b64").write_bytes(cipher)
        log("dry-run：已生成 dryrun.json / dryrun_cipher.b64，未提交。")
        return {"uuid": new_uuid, "dry": True}

    # 4) 取 COS 预签名 URL
    obs = {"bucketName": COS_BUCKET, "method": "Put", "contentType": "text/plain",
           "objectKey": object_key}
    oi = env.biz_data("POST", "/api/obs/temporary/url", obs, timeout=60)
    if isinstance(oi, str):
        oi = json.loads(oi)
    signed_url = oi["signedUrl"]

    # 5) Windows 直传 COS（无需 App 签名）
    import urllib.request
    req = urllib.request.Request(signed_url, data=cipher, method="PUT",
                                 headers={"Content-Type": "text/plain"})
    with urllib.request.urlopen(req, timeout=60) as r:
        log("COS 轨迹上传：%s" % r.status)

    # 6) 提交前 summary 基线（用学期总里程的即时变化判断成绩是否生效，比 records 列表快）
    base = _summary()
    base_total = base.get("semesterDis", 0)
    base_valid = base.get("semesterValidDis", 0)

    # 7) 后台发出 save/record（服务端同步处理约 3–6 分钟，不阻塞本地；本地超时不影响服务器）
    log("提交 save/record，服务端核验中（约 3–6 分钟，工具会自动判定并提示，无需等待）...")
    saver: dict = {}

    def _do_save():
        t0 = time.time()
        try:
            _, r = env.call("POST", HOST + "/api/v70260/runnings/save/record", body, timeout=600)
            saver["resp"] = r
            saver["ms"] = time.time() - t0
        except Exception as e:
            saver["err"] = repr(e)[:120]
            saver["ms"] = time.time() - t0
    threading.Thread(target=_do_save, daemon=True).start()

    (APP_DIR / "last_submit.json").write_text(
        json.dumps({"uuid": new_uuid, "objectKey": object_key, "body": body},
                   ensure_ascii=False, indent=1), encoding="utf-8")

    def _confirm():
        """从 records 取 rrid 后 get_one_record，返回明确判定；查不到返回 None。"""
        recs = _records()
        hit = [r for r in recs if r.get("uuid") == new_uuid]
        if not hit:
            return None
        rrid = hit[0]["rrid"]
        x = env.biz_data("POST", "/api/v70320/runnings/get_one_record",
                         {"uuid": new_uuid, "rrid": rrid, "calculateBadge": False}, timeout=90)
        x = json.loads(x) if isinstance(x, str) else x
        result = {
            "uuid": new_uuid, "rrid": rrid,
            "complete": x.get("complete"), "isCheat": x.get("isCheat"),
            "validDis": x.get("validDis"), "totalDis": x.get("totalDis"),
            "totalTime": x.get("totalTime"), "statusInfo": x.get("statusInfo"),
            "unCompleteReason": x.get("unCompleteReason"),
        }
        log("===== 核验结果（服务端已处理） =====")
        for k in ("rrid", "totalDis", "validDis", "totalTime", "complete",
                  "isCheat", "statusInfo", "unCompleteReason"):
            log("%s = %s" % (k, result.get(k)))
        ok = bool(x.get("complete") and x.get("isCheat") == 0)
        result["success"] = ok
        log("SUCCESS：跑步已成功提交并判定有效。" if ok else
            "CHECK_FAILED：记录已保存但未判定有效，请查看 statusInfo / unCompleteReason。")
        return result

    # 8) 轮询 summary（即时反映），检测成绩生效后再确认。
    #    服务端校验通常 5–12 分钟（不同账号/负载有差异），窗口给足 1000 秒。
    deadline = time.time() + 1000
    announced = False
    while time.time() < deadline:
        time.sleep(6)
        try:
            if not announced:
                log("已提交，等待服务端核验 ...（可去做别的，核验完成会自动提示；也可稍后点“刷新状态”）")
                announced = True
            s = _summary()
            if s.get("semesterDis", 0) > base_total:
                log("服务端已处理（学期总里程 +%.0fm），正在取最终判定 ..." %
                    (s.get("semesterDis", 0) - base_total))
                for _ in range(30):
                    res = _confirm()
                    if res:
                        return res
                    time.sleep(6)
                return {"uuid": new_uuid, "success": True, "processed": True,
                        "note": "成绩已生效，但记录列表同步延迟，稍后可在 App 内查看详情"}
        except Exception as e:
            log("等待核验中 ... %s" % repr(e)[:80])
    log("服务端核验时间较长（uuid=%s）；本次提交已在服务器，稍后点“刷新状态”或重新打开工具会自动确认结果，历史数据显示通常成功。" % new_uuid)
    return {"uuid": new_uuid, "success": False, "polled": True, "pending": True}


def query_status(env: EnvManager) -> dict:
    """返回账号与学期/今日里程信息。"""
    who = env.whoami()
    sid = env.cfg.get("semester_id", 9379)
    summ = env.biz_data("POST", "/api/v55/runnings/recordssummary/semester", {"sid": sid}, timeout=60)
    if isinstance(summ, str):
        summ = json.loads(summ)
    return {"who": who, "summary": summ}


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def cli() -> int:
    _utf8_stream()
    parser = argparse.ArgumentParser(description="运动世界校园一键跑步")
    parser.add_argument("command", nargs="?", default="gui",
                        choices=["gui", "run", "status", "doctor", "dry-run"])
    parser.add_argument("--no-hide", action="store_true", help="不隐藏 MuMu 窗口")
    args = parser.parse_args()

    cfg = load_config()
    if args.no_hide:
        cfg["hide_window_after_launch"] = False

    if args.command == "gui":
        try:
            import tkinter  # noqa: F401
        except Exception:
            print("当前 Python 缺少 Tkinter（精简版 Python 常见），改用控制台菜单。")
            return console_menu(cfg)
        launch_gui()
        return 0

    if args.command == "doctor":
        return cmd_doctor(cfg)

    env = EnvManager(cfg)
    env.prepare()
    if args.command == "status":
        st = query_status(env)
        who = st["who"]
        summ = st["summary"] or {}
        print("当前账号：%s（%s）%s / %s" %
              (who.get("name"), who.get("username"), who.get("campusName"), who.get("gradeClass")))
        for k in ("semesterDis", "semesterValidDis", "targetDis", "todayValidDis", "todayDis"):
            if k in summ:
                print("%s = %s" % (k, summ.get(k)))
        return 0

    if args.command == "dry-run":
        who = env.whoami()
        print("账号：%s（%s）" % (who.get("name"), who.get("username")))
        run_once(env, dry=True)
        return 0

    # run
    who = env.whoami()
    print("账号：%s（%s）%s" % (who.get("name"), who.get("username"), who.get("campusName")))
    res = run_once(env)
    return 0 if res.get("success") else 2


def cmd_doctor(cfg: dict) -> int:
    ok = True
    # 依赖
    try:
        from Crypto.Cipher import AES  # noqa: F401
        print("[OK] 依赖 pycryptodome 已安装")
    except Exception:
        ok = False
        print("[缺失] pycryptodome：请运行  pip install pycryptodome")
    # adb
    adb = str(Path(cfg["mumu_nx_dir"]) / "adb.exe")
    if Path(adb).exists():
        print("[OK] adb：%s" % adb)
    else:
        ok = False
        print("[缺失] adb 不存在：%s（请在 config.json 设置 mumu_nx_dir）" % adb)
    # 数据
    for f in ("sample_trajectory_plain.json", "save_2264_body.json"):
        if (DATA_DIR / f).exists():
            print("[OK] 数据文件 data/%s" % f)
        else:
            ok = False
            print("[缺失] data/%s" % f)
    # 设备
    env = EnvManager(cfg, log=lambda *a: None)
    if Path(adb).exists():
        if env.device_online():
            print("[OK] MuMu/adb 设备在线：%s" % cfg.get("device"))
            try:
                env.ensure_forward()
                env.prepare()
                who = env.whoami()
                print("[OK] 运动世界已登录：%s（%s）" % (who.get("name"), who.get("username")))
                print("[OK] 签名服务可用")
            except Exception as e:
                ok = False
                print("[注意] App/签名服务未就绪：%s" % str(e)[:160])
        else:
            print("[提示] MuMu 未运行，正式运行时会自动后台启动。")
    print("\n自检结果：%s" % ("全部就绪" if ok else "存在上述问题（不影响自动启动项）"))
    return 0 if ok else 1


# ----------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------
def console_menu(cfg: dict) -> int:
    """无 Tkinter 时的控制台交互菜单。"""
    _utf8_stream()
    while True:
        print("\n========== 运动世界校园 · 一键跑步 ==========")
        print("1. 查看状态（连接环境 / 账号 / 里程）")
        print("2. 一键完成当天跑步")
        print("3. 试生成（不提交，仅验证）")
        print("0. 退出")
        try:
            choice = input("请选择：").strip()
        except (EOFError, KeyboardInterrupt):
            return 0
        if choice == "0":
            return 0
        if choice == "1":
            try:
                env = EnvManager(cfg)
                env.prepare()
                st = query_status(env)
                who = st["who"]
                summ = st["summary"] or {}
                print("账号：%s（%s）%s / %s" % (
                    who.get("name"), who.get("username"), who.get("campusName"), who.get("gradeClass")))
                for k in ("semesterDis", "semesterValidDis", "targetDis"):
                    if k in summ:
                        print("%s = %s" % (k, summ.get(k)))
            except Exception as e:
                print("失败：%s" % e)
        elif choice == "2":
            if input("确认提交一次约 2km 跑步？输入 y 继续：").strip().lower() != "y":
                continue
            env = EnvManager(cfg)
            env.prepare()
            who = env.whoami()
            print("账号：%s（%s）" % (who.get("name"), who.get("username")))
            res = run_once(env)
            if res.get("success"):
                print(">>> 成功！有效里程 %s 米" % res.get("validDis"))
            else:
                print(">>> 未确认成功，请看上方日志。")
        elif choice == "3":
            try:
                env = EnvManager(cfg)
                env.prepare()
                env.whoami()
                run_once(env, dry=True)
            except Exception as e:
                print("失败：%s" % e)
        else:
            print("无效选择。")


def launch_gui() -> None:
    import tkinter as tk
    from tkinter import ttk, messagebox

    cfg = load_config()
    root = tk.Tk()
    root.title("运动世界校园 · 一键跑步")
    root.geometry("720x560")
    root.minsize(640, 480)

    top = ttk.Frame(root, padding=(12, 10))
    top.pack(fill="x")
    acct = tk.StringVar(value="账号：未连接")
    quota = tk.StringVar(value="里程：--")
    ttk.Label(top, textvariable=acct, font=("Microsoft YaHei", 11, "bold")).pack(anchor="w")
    ttk.Label(top, textvariable=quota, foreground="#555").pack(anchor="w", pady=(2, 0))

    btns = ttk.Frame(root, padding=(12, 0))
    btns.pack(fill="x")
    run_btn = ttk.Button(btns, text="① 一键完成当天跑步")
    status_btn = ttk.Button(btns, text="刷新状态")
    dry_btn = ttk.Button(btns, text="试生成（不提交）")
    for b in (run_btn, status_btn, dry_btn):
        b.pack(side="left", padx=(0, 8))

    logbox = tk.Text(root, height=20, wrap="word", font=("Consolas", 9))
    sb = ttk.Scrollbar(root, command=logbox.yview)
    logbox.configure(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y", padx=(0, 8), pady=8)
    logbox.pack(fill="both", expand=True, padx=(12, 0), pady=8)

    tip = ttk.Label(root, foreground="#888", padding=(12, 0),
                    text="提示：需在跑步时段 06:00–22:00 提交；首次使用请确保 MuMu 内运动世界已登录、SportSniffer 已启用。")
    tip.pack(anchor="w", pady=(0, 8))

    state = {"busy": False, "env": None}

    def gui_log(msg: str) -> None:
        logbox.insert("end", time.strftime("%H:%M:%S ") + str(msg) + "\n")
        logbox.see("end")
        root.update_idletasks()

    def set_busy(b: bool) -> None:
        state["busy"] = b
        for b_ in (run_btn, status_btn, dry_btn):
            b_.config(state="disabled" if b else "normal")

    def thread(fn, *args) -> None:
        threading.Thread(target=fn, args=args, daemon=True).start()

    def do_refressh() -> None:
        if state["busy"]:
            return
        set_busy(True)
        try:
            env = EnvManager(cfg, gui_log)
            state["env"] = env
            gui_log("正在连接 MuMu / 运动世界 ...")
            env.prepare()
            st = query_status(env)
            who = st["who"]
            summ = st["summary"] or {}
            acct.set("账号：%s（%s）%s / %s" % (
                who.get("name"), who.get("username"), who.get("campusName"), who.get("gradeClass")))
            parts = []
            for k, label in (("semesterDis", "学期总"), ("semesterValidDis", "学期有效"),
                             ("targetDis", "目标")):
                if k in summ:
                    parts.append("%s %s m" % (label, summ.get(k)))
            quota.set("里程：" + "，".join(parts) if parts else "里程：--")
            gui_log("状态已刷新。")
        except Exception as e:
            gui_log("刷新失败：%s" % e)
        finally:
            set_busy(False)

    def do_run(dry: bool) -> None:
        if state["busy"]:
            return
        if not dry and not messagebox.askyesno(
                "确认", "将自动提交一次约 2km 的合规跑步记录，是否继续？"):
            return
        set_busy(True)

        def work():
            try:
                env = EnvManager(cfg, gui_log)
                state["env"] = env
                gui_log("正在连接 MuMu / 运动世界 ...")
                env.prepare()
                who = env.whoami()
                acct.set("账号：%s（%s）" % (who.get("name"), who.get("username")))
                gui_log("账号：%s（%s）" % (who.get("name"), who.get("username")))
                res = run_once(env, gui_log, dry=dry)
                if dry:
                    messagebox.showinfo("完成", "试生成完成，未提交。")
                elif res.get("success") and res.get("already"):
                    messagebox.showinfo("今天已完成", "今天的跑步此前已完成并判定有效，无需重复提交。\n有效里程 %s 米" %
                                        res.get("validDis"))
                elif res.get("success"):
                    messagebox.showinfo("成功", "当天跑步已成功提交并判定有效！\n有效里程 %s 米" %
                                        res.get("validDis"))
                elif res.get("pending"):
                    messagebox.showinfo("核验中", "跑步已成功提交到服务器，服务端核验通常需要 5–12 分钟。\n稍后点“刷新状态”或重新打开工具，会自动确认结果，无需重新提交。")
                elif res.get("polled"):
                    messagebox.showwarning("结果", "提交已完成，但暂未查询到判定结果，可稍后在 App 查看。")
            except Exception as e:
                gui_log("出错：%s" % e)
                messagebox.showerror("出错", str(e))
            finally:
                set_busy(False)

        thread(work)

    run_btn.config(command=lambda: do_run(False))
    dry_btn.config(command=lambda: do_run(True))
    status_btn.config(command=lambda: thread(do_refressh))

    # 启动时自动刷新一次（后台）
    root.after(300, lambda: thread(do_refressh))
    root.mainloop()


if __name__ == "__main__":
    raise SystemExit(cli())
