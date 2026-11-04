# -*- coding: utf-8 -*-
"""离线自测：不联网，用假客户端跑完整链路。

覆盖：
  1. NetSec 信封往返（加密 → 模拟服务端用响应密钥回包 → 解密）；
  2. 轨迹合成 + 一致性校验（里程/时间/打卡点）；
  3. 报文与签名（31 字段 / 分桶汇总 / OBS 载荷）；
  4. Runner 全流程（dry-run 与真实提交 + 核验）。
"""
from __future__ import annotations

import json
import math
import time
import uuid

import random as _random

from . import policy, protocol, store, track
from .runner import Report, Runner


STRICT_RULE = {
    "policy": 1,
    "runRuleModel": {
        "minDistance": 2000, "minRunTime": 900, "dayGoal": 3000, "maxRunTime": 7200,
        "speedTop": 2.5, "speedBottom": 3.5,
        "validTime": [{"start": "06:00:00", "end": "22:00:00"}],
    },
    "runAreaModels": [{"id": 77, "valid": True, "select": True, "fenceIds": [1]}],
}


class StrictClientMixin:
    def run_policy(self, run_mode: int = 1, unid=None):
        return dict(STRICT_RULE)


class FakeClient:
    """内存假客户端：结构与 SportClient 对齐，返回构造好的服务端响应。"""

    def __init__(self, strict: bool = False) -> None:
        self.strict = strict
        self.saved = None
        self.rrid = 987654321
        self.valid = True
        self.t0 = int(time.time() * 1000)

    # 时间
    def sync_time(self):
        return self.now_ms()

    def now_ms(self):
        return int(time.time() * 1000)

    # 账号
    def user_info(self):
        return {"uid": 10001, "unid": 3502, "name": "测试同学", "username": "138xxxxxxxx",
                "campusName": "测试大学", "gradeClass": "2024级1班"}

    # 规则/围栏
    def run_policy(self, run_mode: int = 1, unid=None):
        if getattr(self, "strict", False):
            return dict(STRICT_RULE)
        return {
            "policy": 1,
            "runRuleModel": {
                "minDistance": 2000, "minRunTime": 600, "dayGoal": 2000, "maxRunTime": 7200,
                "speedTop": "10'00\"", "speedBottom": "3'00\"",
                "validTime": [{"start": "06:00", "end": "22:00"}],
            },
            "runAreaModels": [{"id": 77, "valid": True, "select": True, "fenceIds": [1]}],
        }

    def geo_fences(self):
        ring = []
        for i in range(12):
            ang = 2 * math.pi * i / 12
            ring.append({"glat": 22.9057 + 0.0016 * math.cos(ang),
                         "glon": 113.8746 + 0.0016 * math.sin(ang)})
        return [{"id": 1, "name": "测试校区围栏", "points": ring}]

    def points(self, distance_m, lat, lon, run_area_id=-1, run_uuid=None):
        pts = []
        for i in range(5):
            ang = 2 * math.pi * i / 5
            pts.append({"id": 900 + i, "lat": lat + 0.0012 * math.cos(ang),
                        "lng": lon + 0.0012 * math.sin(ang),
                        "pointName": "打卡点%d" % (i + 1), "isFixed": 1, "position": i + 1})
        return {"pointsResModels": pts, "sportStartTime": self.now_ms()}

    # 记录
    def records(self, page=1, size=10, sid=0, sport_type=1):
        return []

    def today_records(self):
        return []

    def semester_summary(self, sid: int = 0):
        return {"semesterDis": 12000, "semesterValidDis": 11800, "targetDis": 60000}

    def semester_info(self, run_mode: int = 1):
        return {"totalDistance": 12000, "targetDistance": 60000}

    # 提交链路
    def obs_url(self, object_key):
        return {"signedUrl": "https://obs.example.com/put/%s" % object_key,
                "actualSignedRequestHeaders": {"Content-Type": "text/plain"}}

    def put_object(self, url, payload, headers=None):
        self.last_payload = payload
        return 200

    def save_record(self, body):
        self.saved = body
        return {"rrid": self.rrid}

    def verify_submission(self, run_uuid=""):
        return {"record": {"rrid": self.rrid, "uuid": run_uuid, "complete": True},
                "detail": {"complete": True, "isCheat": 0, "validDis": 2100,
                           "statusInfo": "太棒了！恭喜你，本次跑步挑战成功", "jsonFromObs": 1},
                "today": [], "semester": {"semesterDis": 12000, "semesterValidDis": 11800}}

    def one_record(self, rrid):
        # 与真实返回对齐：成功时 status 仍是 0，靠 complete/isCheat/validDis 判断
        if self.valid:
            return {"rrid": rrid, "status": 0, "complete": True, "isCheat": 0,
                    "unCompleteReason": 0, "validDis": 2100, "totalDis": 2100,
                    "totalTime": 780, "statusInfo": "太棒了！恭喜你，本次跑步挑战成功"}
        return {"rrid": rrid, "status": 0, "complete": False, "isCheat": 0,
                "unCompleteReason": 10, "validDis": 0, "statusInfo": "未达最低里程"}


def _isolate_paths():
    """自测把 BASE_DIR/LOG_DIR 指到临时目录，避免在发行包里留下 dryrun.json / logs。"""
    import tempfile
    from pathlib import Path as _P
    tmp = _P(tempfile.mkdtemp(prefix="sportwin-selftest-"))
    orig = (store.BASE_DIR, store.LOG_DIR)
    store.BASE_DIR = tmp
    store.LOG_DIR = tmp / "logs"
    store.LOG_DIR.mkdir(parents=True, exist_ok=True)
    return orig, tmp


def _patch_store():
    """自测不能碰真实 state/credentials：全部换成内存空实现。"""
    names = ("save_credentials", "mark_submitted", "mark_result", "save_state",
             "submit_lock", "load_state", "update_submit", "last_submit")
    orig = {n: getattr(store, n) for n in names}
    store.save_credentials = lambda *a, **k: {}
    store.mark_submitted = lambda *a, **k: None
    store.mark_result = lambda *a, **k: None
    store.save_state = lambda *a, **k: None
    store.submit_lock = lambda *a, **k: None
    store.load_state = lambda *a, **k: {}
    store.update_submit = lambda *a, **k: None
    store.last_submit = lambda *a, **k: {}
    return orig


def run() -> bool:
    ok = True

    def check(name, cond, extra=""):
        nonlocal ok
        print(("  [OK]   " if cond else "  [FAIL] ") + name + ("  " + str(extra) if extra else ""))
        if not cond:
            ok = False

    print("1) 密码学 / 信封")
    check("AES 自检", protocol.self_test(verbose=False))
    biz = {"error": 10000, "data": {"hello": "世界", "n": 1}}
    env = protocol.Envelope.wrap({"k": "v"})
    inner = json.dumps({"data": __import__("base64").b64encode(
        json.dumps(biz, ensure_ascii=False).encode()).decode()}, ensure_ascii=False)
    from .protocol import aes128_cbc_encrypt
    import base64
    r = base64.b64encode(aes128_cbc_encrypt(inner.encode(), env.resp_key)).decode()
    check("信封往返", env.unwrap({"r": r}) == biz)
    check("headerSign 五字段", set(env.wire) == {"d", "h", "k", "p", "t"})

    print("2) 轨迹合成（v3：真实采样/停顿/噪声）")
    ring = [(22.9057 + 0.0016 * math.cos(2 * math.pi * i / 12),
             113.8746 + 0.0016 * math.sin(2 * math.pi * i / 12)) for i in range(12)]
    cps_pre = [{"lat": ring[i % len(ring)][0], "lng": ring[i % len(ring)][1]} for i in range(5)]
    tr = track.sample_track(track.build_route(ring), 2100, 2.6, 0, 172,
                            run_cfg=json.loads(json.dumps(store.DEFAULT_CONFIG["run"])),
                            checkpoints=cps_pre)
    gps = tr.points
    check("采样点数量", 200 < len(gps) < 4000, len(gps))
    check("字段齐全(27 项)", not (set(track.CLIENT_POINT_KEYS) - set(gps[0].keys())),
          sorted(set(track.CLIENT_POINT_KEYS) - set(gps[0].keys())) or "完整")
    check("首点 gainTime", track.parse_gain_time(gps[0]["gainTime"]) == 0)
    check("末点 gainTime", abs(track.parse_gain_time(gps[-1]["gainTime"]) - tr.duration_s) <= 2,
          "%s vs %s" % (track.parse_gain_time(gps[-1]["gainTime"]), tr.duration_s))
    check("gLat/gLng 有效", all(p["gLat"] > 0 and p["gLng"] > 0 for p in gps))
    check("lat/lng = -1（gcj02 约定）", all(p["lat"] == -1.0 and p["lng"] == -1.0 for p in gps))
    check("type 含起终点/打卡点", all(t in {p["type"] for p in gps} for t in (5, 6)),
          str({p["type"] for p in gps}))
    actual = sum(track.distance((a["gLat"], a["gLng"]), (b["gLat"], b["gLng"])) for a, b in zip(gps, gps[1:]))
    check("几何与 totalDis 一致", abs(actual - tr.distance_m) < 3, "%.1fm" % actual)
    check("里程接近目标", abs(tr.distance_m - 2100) / 2100 < 0.05, "%.1fm" % tr.distance_m)
    check("radius 有真实分布", max(p["radius"] for p in gps) - min(p["radius"] for p in gps) > 0.8,
          "%.2f–%.2f" % (min(p["radius"] for p in gps), max(p["radius"] for p in gps)))
    check("totalDis 递增", all(b["totalDis"] >= a["totalDis"] for a, b in zip(gps, gps[1:])))

    print("3) 报文与签名（含 10 秒分桶真实性）")
    cps = [{"lat": ring[i % len(ring)][0], "lng": ring[i % len(ring)][1],
            "pointName": "P%d" % i, "isFixed": 1, "isPass": True, "position": i + 1} for i in range(5)]
    body, obs, key = track.build_record(
        uid=10001, unid=3502, run_uuid=str(uuid.uuid4()), sport_type=1,
        distance_m=int(round(tr.distance_m)), duration_s=tr.duration_s, steps=tr.steps,
        start_ms=0, stop_ms=tr.duration_s * 1000, gps=gps,
        checkpoints=cps, route_vertices=tr.path, sel_distance=2000, sel_runtime=600, seed=7)
    check("allLocJson 为 27 字段", set(json.loads(body["allLocJson"])[0].keys()) == set(track.CLIENT_POINT_KEYS))
    check("signature 32 位", len(body["signature"]) == 32, body["signature"])
    check("originalSign 31 字段", body["originalSign"].count("&") == 30,
          body["originalSign"].count("&") + 1)
    bd = [b["distance"] for b in body["speedPerTenSec"]]
    bs = [b["stepsNum"] for b in body["stepsPerTenSec"]]
    mean_d = sum(bd) / len(bd)
    sd_d = (sum((x - mean_d) ** 2 for x in bd) / len(bd)) ** 0.5
    check("分桶距离汇总=总里程", abs(sum(bd) - int(round(tr.distance_m))) <= 1.0, sum(bd))
    check("分桶距离有真实波动(σ>0.5m)", sd_d > 0.5, "σ=%.2f" % sd_d)
    check("分桶步数汇总=总步数", sum(bs) == tr.steps, sum(bs))
    check("分桶步数有波动", len(set(bs)) >= 5, "%d 种" % len(set(bs)))
    check("OBS 11 键", len(obs) == 11, len(obs))
    check("对象 key 结构", key.startswith("run_data/") and key.endswith(".json"))

    print("4) 合规随机（里程/配速/步频每次都不同且合法）")
    for label, rule in (
        ("宽松校规", {"minDistance": 1000, "minRunTime": 0, "speedTop": 2.0, "speedBottom": 10.0,
                     "stepTop": 300, "stepBottom": 60, "dayGoal": 2000}),
        ("严格校规", {"minDistance": 2000, "minRunTime": 900, "speedTop": 2.5, "speedBottom": 3.5,
                     "stepTop": 180, "stepBottom": 150, "dayGoal": 2000}),
    ):
        lim = policy.Limits.from_policy({"policy": 1, "runRuleModel": rule})
        rng = _random.Random(7)
        plans, bad = [], 0
        for _ in range(60):
            try:
                pl = policy.choose_run(lim, {"distance_range_m": [2000, 2200],
                                             "pace_range_sec_per_km": [360, 430],
                                             "cadence_range_spm": [166, 182]}, rng)
                if policy.validate(pl, lim):
                    bad += 1
                plans.append(pl)
            except Exception:
                bad += 1
        ok_plan = bool(plans) and bad == 0
        check("%s：60 次抽样全部合规" % label, ok_plan, "非法 %d" % bad)
        check("%s：里程/配速/步频有差异" % label,
              len({p.distance_m for p in plans}) >= 3 and len({round(p.pace_sec) for p in plans}) >= 3
              and len({round(p.cadence) for p in plans}) >= 3,
              "里程%d种/配速%d种/步频%d种" % (
                  len({p.distance_m for p in plans}), len({round(p.pace_sec) for p in plans}),
                  len({round(p.cadence) for p in plans})))

    print("5) Runner 全流程（假服务端）")
    _orig_paths, _tmpdir = _isolate_paths()
    saved = _patch_store()
    try:
        cfg = json.loads(json.dumps(store.DEFAULT_CONFIG))
        r = Runner.__new__(Runner)
        r.cfg = cfg
        r.log = lambda m: None
        r.client = FakeClient()
        r.run_cfg = cfg["run"]
        r.account_key = "selftest"
        r.report = Report(ok=False, status="init")
        rep = r.run(dry=True)
        check("dry-run 成功", rep.ok and rep.status == "dry", rep.message)
        check("dryrun.json 落盘", (store.BASE_DIR / "dryrun.json").exists())

        r2 = Runner.__new__(Runner)
        r2.cfg = cfg
        r2.log = lambda m: None
        r2.client = FakeClient()
        r2.run_cfg = cfg["run"]
        r2.account_key = "selftest"
        r2.report = Report(ok=False, status="init")
        rep2 = r2.run(dry=False, poll_sec=10)
        check("真实提交并核验", rep2.ok and rep2.status == "done", rep2.message)
        check("rrid 回填", rep2.rrid == r2.client.rrid, rep2.rrid)
        check("提交体已生成", isinstance(r2.client.saved, dict) and "signature" in r2.client.saved)
    finally:
        for _n, _v in saved.items():
            setattr(store, _n, _v)
        store.BASE_DIR, store.LOG_DIR = _orig_paths

    print("6) 严格校规端到端（min2000m / minRunTime900s / 速度2.5–3.5）")
    _orig2, _tmp2 = _isolate_paths()
    saved2 = _patch_store()
    try:
        cfg2 = json.loads(json.dumps(store.DEFAULT_CONFIG))
        r3 = Runner.__new__(Runner)
        r3.cfg = cfg2
        r3.log = lambda m: None
        r3.client = FakeClient(strict=True)
        r3.run_cfg = cfg2["run"]
        r3.account_key = "selftest"
        r3.report = Report(ok=False, status="init")
        rep3 = r3.run(dry=True)
        check("严格校规 dry-run 成功", rep3.ok, rep3.message)
        check("里程≥学校下限", rep3.distance_m >= 2000, "%.0fm" % rep3.distance_m)
        check("用时≥minRunTime", rep3.duration_s >= 900, "%ds" % rep3.duration_s)
    finally:
        for _n, _v in saved2.items():
            setattr(store, _n, _v)
        store.BASE_DIR, store.LOG_DIR = _orig2

    print("\n自测结果：%s" % ("全部通过" if ok else "存在失败项"))
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if run() else 1)
