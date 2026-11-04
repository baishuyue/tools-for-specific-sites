# -*- coding: utf-8 -*-
"""本地状态：配置、凭据（DPAPI 加密）、幂等锁、日志。

凭据落盘用 Windows DPAPI（CryptProtectData）加密，只有当前 Windows 用户能解开，
避免 token 明文躺在磁盘上。非 Windows 环境退化为机器绑定异或混淆。
"""
from __future__ import annotations

import base64
import ctypes

try:                      # 仅 Windows 有；其它平台走 XOR 兜底
    import ctypes.wintypes as wt
except Exception:         # pragma: no cover
    wt = None
import datetime
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

APP_NAME = "sportwin"

import sys as _sys

if getattr(_sys, "frozen", False):
    # PyInstaller 单文件：__file__ 在临时解包目录里，配置必须放在 exe 旁边
    BASE_DIR = Path(_sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent.parent

_BUNDLE_DIR = Path(getattr(_sys, "_MEIPASS", BASE_DIR))
#: 服务端按中国时间判定（有效时段、日额度重置都在 UTC+8）
SERVER_TZ = datetime.timezone(datetime.timedelta(hours=8))
CONFIG_PATH = BASE_DIR / "config.json"
CRED_PATH = BASE_DIR / "credentials.bin"
STATE_PATH = BASE_DIR / "state.json"
LOG_DIR = BASE_DIR / "logs"
DATA_DIR = BASE_DIR / "data"
if not DATA_DIR.exists():          # 打包后 data 在 _MEIPASS 里
    DATA_DIR = _BUNDLE_DIR / "data"

DEFAULT_CONFIG: dict = {
    "device": {
        "deviceName": "Android Device",
        "osVersion": "13",
        "physicPixel": "1080x2400",
        "logicPixel": "393x873",
        "cpuModel": "arm64-v8a",
        "per_account": True,   # 每个账号一套设备指纹（避免"同设备多账号"被关联）
        "appVersion": "7.3.92",# App 版本（协议升级时改这里）
    },
    "run": {
        "sport_type": 1,                    # 1=计分跑（校园考核） 6=自由跑
        # ---- 每次随机区间（都会先与学校规则求交集，越界直接拒绝）----
        "distance_range_m": [2000, 2200],   # 单次里程随机区间
        "pace_range_sec_per_km": [360, 430],# 配速区间：6'00"–7'10" /km
        "cadence_range_spm": [166, 182],    # 步频区间：步/分
        "stop_offset_sec": [0, 1800],       # 结束时刻相对"现在"的随机前置秒数（0–30 分钟）
        # ---- 物理可信区间（防止生成"机器数据"，与校规取交集）----
        "human_speed_mps": [1.8, 4.2],      # 6'40"–9'15"/km，再快就不像学生跑了
        "human_cadence_spm": [150, 190],
        "human_stride_m": [0.65, 1.35],
        "max_runs_per_day": 1,              # 同账号每天最多提交几次
        "allow_frozen": False,              # 服务端下发 freezeRunTime / 无规则时是否强行提交
        # ---- 固定值（>0 时覆盖对应随机区间，调试/复现用）----
        "distance_m": 0,
        "duration_sec": 0,
        "steps_per_km": 1120,               # 兜底步频基数
        "pace_variance": 0.06,              # 轨迹内速度抖动（旧参数，保留兼容）
        # ---- 轨迹真实性（v3：让数据像传感器采出来的）----
        "sample_interval_s": [0.9, 1.6],    # GPS 采样间隔
        "gps_gap_prob": 0.08,               # 丢点概率
        "gps_gap_s": [3.0, 9.0],            # 丢点持续时间
        "gps_noise_m": [1.5, 6.0],          # 位置噪声（米）
        "accuracy_range": [3.0, 45.0],      # accuracy 分布范围
        "pause_prob": 0.85,                 # 是否插入停顿（概率越高越像真人）
        "pause_sec": [8.0, 45.0],           # 每次停顿时长
        "speed_noise": 0.07,                # speed 字段与几何解耦的噪声
        "path_smooth": 2,                   # Chaikin 平滑次数
        "path_seg_m": 8.0,                  # 路径加密步长
        "run_area_id": -1,
        "allow_template_checkpoints": False,  # 仅当本校区与 data/ 模板同校区时才打开
    },
    "net": {"timeout": 15, "retries": 3, "proxy": "", "insecure": False},
    "login": {
        "phone": "",              # 记住上次登录的手机号（方便下次一键登录）
        "captcha_mode": "browser",# browser=浏览器拖一次滑块 / api=打码平台全自动
        "captcha_api_key": "",    # 2captcha 兼容平台的 key（可选）
        "captcha_timeout": 300,
        "captcha_id": "",         # 留空用内置值；协议升级时在这里覆盖
    },
    "ui": {"hide_console": False, "auto_refresh": True},
}

DATA_SAMPLE = DATA_DIR / "sample_trajectory_plain.json"
DATA_BODY = DATA_DIR / "save_2264_body.json"


# ---------------------------------------------------------------------------
# DPAPI
# ---------------------------------------------------------------------------
class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    if os.name != "nt" or wt is None:
        raise OSError("DPAPI 仅 Windows 可用")
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _Blob()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    args = [ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)]
    if not fn(*args):
        raise OSError("DPAPI 调用失败")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _machine_key() -> bytes:
    seed = "%s|%s|%s" % (APP_NAME, os.environ.get("USERNAME", "u"), os.environ.get("COMPUTERNAME", "h"))
    return hashlib.sha256(seed.encode("utf-8")).digest()


def _xor(data: bytes) -> bytes:
    key = _machine_key()
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))


def seal(data: dict) -> str:
    raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
    try:
        blob = _dpapi(raw, True)
        return "dpapi:" + base64.b64encode(blob).decode("ascii")
    except Exception:
        return "xor:" + base64.b64encode(_xor(raw)).decode("ascii")


def unseal(text: str) -> dict:
    text = text.strip()
    if text.startswith("dpapi:"):
        return json.loads(_dpapi(base64.b64decode(text[6:]), False).decode("utf-8"))
    if text.startswith("xor:"):
        return json.loads(_xor(base64.b64decode(text[4:])).decode("utf-8"))
    return json.loads(text)  # 兼容明文旧格式


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def deep_merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if CONFIG_PATH.exists():
        try:
            cfg = deep_merge(cfg, json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    # 旧版 config.json（固定 distance_m / duration_sec）→ 自动迁移成随机区间，只做一次
    if CONFIG_PATH.exists():
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            run_raw = raw.get("run") or {}
            if "distance_range_m" not in run_raw:
                d = int(run_raw.get("distance_m") or 0)
                t = int(run_raw.get("duration_sec") or 0)
                if d > 0:
                    run_raw["distance_range_m"] = [max(d - 100, 0), d + 100]
                if d > 0 and t > 0:
                    pace = t / (d / 1000.0)
                    run_raw["pace_range_sec_per_km"] = [int(round(pace * 0.95)),
                                                       int(round(pace * 1.10))]
                run_raw["distance_m"] = 0
                run_raw["duration_sec"] = 0
                raw["run"] = run_raw
                CONFIG_PATH.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
                cfg = deep_merge(cfg, run_raw)
        except Exception:
            pass

    run_cfg = cfg.setdefault("run", {})
    if run_cfg.get("stop_offset_sec") == [0, 25]:
        run_cfg["stop_offset_sec"] = [0, 1800]      # 旧默认太窄，升级为 0–30 分钟
        save_config(cfg)

    dev = cfg.setdefault("device", {})
    if not dev.get("DeviceId"):
        dev["DeviceId"] = hashlib.md5(
            ("%s-%s" % (os.environ.get("COMPUTERNAME", "pc"), time.time())).encode()).hexdigest()
        save_config(cfg)
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# 凭据（多账号账本）
# ---------------------------------------------------------------------------
def _load_book() -> dict:
    if not CRED_PATH.exists():
        return {"version": 2, "current": "", "accounts": {}}
    try:
        raw = unseal(CRED_PATH.read_text(encoding="ascii"))
    except Exception:
        return {"version": 2, "current": "", "accounts": {}}
    if not isinstance(raw, dict):
        return {"version": 2, "current": "", "accounts": {}}
    if "accounts" in raw:                      # 新格式
        raw.setdefault("version", 2)
        raw.setdefault("current", "")
        raw["accounts"] = raw.get("accounts") or {}
        return raw
    # 旧格式（单账号扁平结构）→ 迁移
    if raw.get("token"):
        key = str(raw.get("uid") or raw.get("phone") or "default")
        acc = dict(raw)
        acc.setdefault("uid", key)
        return {"version": 2, "current": key, "accounts": {key: acc}}
    return {"version": 2, "current": "", "accounts": {}}


def _save_book(book: dict) -> None:
    CRED_PATH.write_text(seal(book), encoding="ascii")
    try:
        os.chmod(CRED_PATH, 0o600)
    except Exception:
        pass


def account_key(uid: Any = None, phone: str = "") -> str:
    uid_s = str(uid or "").strip()
    if uid_s and uid_s not in ("-1", "None"):
        return uid_s
    return (phone or "").strip()


def list_accounts() -> list:
    """[{key, uid, unid, phone, name, campus, updated_at, current}]"""
    book = _load_book()
    cur = book.get("current")
    out = []
    for key, acc in (book.get("accounts") or {}).items():
        item = dict(acc)
        item["key"] = key
        item["current"] = (key == cur)
        out.append(item)
    out.sort(key=lambda a: str(a.get("updated_at", "")), reverse=True)
    return out


def find_account(ident: str) -> dict:
    """按 uid / 手机号 / key 查账号。"""
    ident = str(ident or "").strip()
    if not ident:
        return {}
    for a in list_accounts():
        if ident in (str(a.get("key") or ""), str(a.get("uid") or ""), str(a.get("phone") or "")):
            return a
    return {}


def _update_account(key: str, **fields: Any) -> None:
    book = _load_book()
    accounts = book.get("accounts") or {}
    if key not in accounts:
        return
    accounts[key].update({k: v for k, v in fields.items() if v})
    _save_book(book)


def current_key() -> str:
    return str(_load_book().get("current") or "")


def set_account(key: str) -> bool:
    book = _load_book()
    if key not in (book.get("accounts") or {}):
        return False
    book["current"] = key
    _save_book(book)
    return True


def remove_account(key: str) -> bool:
    book = _load_book()
    accounts = book.get("accounts") or {}
    if key not in accounts:
        return False
    accounts.pop(key, None)
    if book.get("current") == key:
        book["current"] = next(iter(accounts), "")
    _save_book(book)
    return True


def load_credentials(key: Optional[str] = None) -> dict:
    """当前账号（或指定 key）的凭据；账号不存在返回 {}。"""
    book = _load_book()
    accounts = book.get("accounts") or {}
    use = key or book.get("current") or ""
    acc = dict(accounts.get(use) or {})
    if acc:
        acc.setdefault("uid", use)
        acc["key"] = use
    return acc


def save_credentials(uid: Any = None, token: Optional[str] = None, unid: Any = None,
                     source: str = "manual", phone: str = "", name: str = "",
                     campus: str = "", key: Optional[str] = None,
                     device_id: str = "") -> dict:
    """新增/更新账号并切换为当前账号（token 为空时只更新资料字段）。"""
    book = _load_book()
    accounts = book.get("accounts") or {}
    use = key or account_key(uid, phone) or book.get("current")
    if not use:
        return {}
    acc = dict(accounts.get(use) or {})
    if uid not in (None, "", "-1", -1):
        acc["uid"] = str(uid)
    if token:
        acc["token"] = str(token)
    if unid not in (None, "", "-1", -1):
        acc["unid"] = str(unid)
    if phone:
        acc["phone"] = str(phone)
    if name:
        acc["name"] = str(name)
    if campus:
        acc["campus"] = str(campus)
    if device_id:
        acc["device_id"] = str(device_id)
    if source:
        acc["source"] = source
    acc["updated_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    accounts[use] = acc
    book["accounts"] = accounts
    book["current"] = use
    book["version"] = 2
    _save_book(book)
    acc["key"] = use
    return acc


def clear_credentials(key: Optional[str] = None) -> None:
    if key:
        remove_account(key)
        return
    if CRED_PATH.exists():
        CRED_PATH.unlink()


def device_for(cfg: dict, account: Optional[dict] = None) -> dict:
    """按账号返回设备指纹：per_account=True 时每个账号一个稳定且持久的 DeviceId。

    首次计算后写回账号（device_id），之后即使补了手机号/改了 uid 也不会漂移。
    """
    dev = dict((cfg or {}).get("device") or {})
    acc = dict(account if account is not None else load_credentials())
    if dev.get("per_account", True):
        stored = str(acc.get("device_id") or "")
        if stored:
            dev["DeviceId"] = stored
        else:
            # 单账号用户：沿用原来的整机指纹，避免"升级后变新设备"触发风控
            machine = (cfg.get("device") or {}).get("DeviceId")
            seed_val = acc.get("phone") or acc.get("key") or acc.get("uid") or ""
            new_id = ""
            if machine and len(list_accounts()) <= 1:
                new_id = str(machine)
            elif seed_val:
                new_id = hashlib.md5(("%s|%s" % (APP_NAME, seed_val)).encode("utf-8")).hexdigest()
            if new_id:
                dev["DeviceId"] = new_id
                key = acc.get("key") or acc.get("uid") or acc.get("phone")
                if key:
                    _update_account(str(key), device_id=new_id)
    if not dev.get("DeviceId"):
        dev["DeviceId"] = hashlib.md5(("%s|%s" % (
            os.environ.get("COMPUTERNAME", "pc"), os.environ.get("USERNAME", "u"))).encode()).hexdigest()
    return dev


# ---------------------------------------------------------------------------
# 幂等锁 / 当日状态（按账号隔离）
# ---------------------------------------------------------------------------
def now_server() -> datetime.datetime:
    """服务端时钟（UTC+8），与本机时区无关。"""
    return datetime.datetime.now(SERVER_TZ)


def today_key(ts_ms: Optional[int] = None) -> str:
    """服务端日期（按 UTC+8 切日）。"""
    if ts_ms is None:
        return now_server().strftime("%Y-%m-%d")
    return datetime.datetime.fromtimestamp(ts_ms / 1000.0, SERVER_TZ).strftime("%Y-%m-%d")


def server_midnight_ms() -> int:
    """服务端"今天 00:00"的毫秒时间戳。"""
    d = now_server().replace(hour=0, minute=0, second=0, microsecond=0)
    return int(d.timestamp() * 1000)


def _load_state_book() -> dict:
    """state.json 结构：{current, accounts:{uid:{last_submit,last_result,...}}}

    旧版是扁平结构（全局一份），迁移时归到当时的当前账号，避免多账号串数据。
    """
    if not STATE_PATH.exists():
        return {"current": current_key(), "accounts": {}}
    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"current": current_key(), "accounts": {}}
    if not isinstance(raw, dict):
        return {"current": current_key(), "accounts": {}}
    if "accounts" in raw:
        raw.setdefault("current", current_key())
        raw["accounts"] = raw.get("accounts") or {}
        return raw
    if not raw:
        return {"current": current_key(), "accounts": {}}
    key = current_key() or "default"          # 旧扁平结构 → 归给当前账号
    book = {"current": key, "accounts": {key: raw}}
    _save_state_book(book)                    # 立刻落盘，避免下次读取时又按"当前账号"重算
    return book


def _save_state_book(book: dict) -> None:
    STATE_PATH.write_text(json.dumps(book, ensure_ascii=False, indent=2), encoding="utf-8")


def _state_of(book: dict, account: Optional[str] = None) -> dict:
    key = account or book.get("current") or current_key() or "default"
    return dict((book.get("accounts") or {}).get(key) or {})


def state_account(account: Optional[str] = None) -> str:
    return account or current_key() or "default"


def load_state(account: Optional[str] = None) -> dict:
    """该账号的状态（没有就返回空 dict）。"""
    book = _load_state_book()
    return dict(_state_of(book, account))


def save_state(state: dict, account: Optional[str] = None) -> None:
    book = _load_state_book()
    key = state_account(account)
    book.setdefault("accounts", {})[key] = dict(state or {})
    book["current"] = key
    _save_state_book(book)


def submit_lock(within_sec: int = 1200, account: Optional[str] = None) -> Optional[dict]:
    """最近一次提交仍在核验窗口内则返回该记录，避免重复上报（仅限本账号）。"""
    st = load_state(account).get("last_submit") or {}
    if not st:
        return None
    if st.get("date") != today_key():
        return None
    if time.time() - float(st.get("at", 0)) > within_sec:
        return None
    return st


def mark_submitted(uuid: str, rrid: Any, distance: int, duration: int,
                   account: Optional[str] = None, **extra: Any) -> None:
    st = load_state(account)
    rec = {
        "uuid": uuid,
        "rrid": rrid,
        "distance": distance,
        "duration": duration,
        "at": time.time(),
        "date": today_key(),
        "account": state_account(account),
    }
    rec.update(extra or {})
    st["last_submit"] = rec
    save_state(st, account)


def update_submit(account: Optional[str] = None, **fields: Any) -> None:
    """补写/更新本账号最近一次提交的字段（上传结果、判定结果、学期基线等）。"""
    st = load_state(account)
    rec = dict(st.get("last_submit") or {})
    if not rec:
        return
    rec.update(fields or {})
    st["last_submit"] = rec
    save_state(st, account)


def last_submit(account: Optional[str] = None) -> dict:
    return dict(load_state(account).get("last_submit") or {})


def mark_result(ok: bool, detail: dict, account: Optional[str] = None) -> None:
    st = load_state(account)
    st["last_result"] = {"ok": bool(ok), "at": time.time(), "detail": detail}
    if ok:
        st["last_success_date"] = today_key()
    save_state(st, account)


def clear_submit(account: Optional[str] = None) -> None:
    """丢掉本账号的 last_submit（比如确认那条记录根本不属于本账号）。"""
    st = load_state(account)
    st.pop("last_submit", None)
    save_state(st, account)


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
class Logger:
    def __init__(self, echo=True) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.path = LOG_DIR / ("sport-%s.log" % datetime.date.today().strftime("%Y%m%d"))
        self.echo = echo
        self.sink = None  # GUI 可注入回调

    def __call__(self, msg: str) -> None:
        line = "%s %s" % (datetime.datetime.now().strftime("%H:%M:%S"), str(msg).rstrip())
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            pass
        if self.sink:
            try:
                self.sink(line)
            except Exception:
                pass
        if self.echo:
            try:
                print(line, flush=True)
            except Exception:
                pass


def ensure_utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
