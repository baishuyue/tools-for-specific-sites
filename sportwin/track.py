# -*- coding: utf-8 -*-
"""轨迹合成与结算报文构造（v3：面向"运动数据真实性"）。

v2 的问题（被服务端判 `运动数据异常` 的直接原因）：
  * 10 秒分桶按比例线性切分 → 距离标准差 0.005 m、步数 30/30/31…，真实传感器不可能这么整齐；
  * 采样间隔固定 3 秒、accuracy 恒定 2.5–3.2、相邻位移 6.93 m 极均匀；
  * speed 字段与 Δ距离/Δ时间 严格相等（真实客户端 speed 来自 GPS，有偏差）；
  * 路径是 5 个打卡点的直线弦，形状过于规整。

v3 改法：
  1. Chaikin 平滑 + 加密，路径变成带圆角的连续曲线；
  2. 采样间隔随机（0.9–1.6s，8% 概率 GPS 丢点 3–9s）；
  3. 插入 0–3 次真实停顿（8–45s，speed≈0，此时不产生距离/步数）；
  4. 位置加相关噪声（低频摆动 + 每点抖动），accuracy 拉成真实分布（偶发 25–60m）；
  5. speed 字段 = 位移/时间 × 噪声（0.94–1.06），与几何解耦；
  6. 10 秒分桶按"实际分段长度 × 时间重叠"统计，不再比例切分，步数按移动时间 + 抖动分配；
  7. 最终距离/时长/步数由几何反算，三者天然自洽，再由 policy.validate 复核。
"""
from __future__ import annotations

import base64
import gzip
import json
import math
import random
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import protocol

EARTH_R = 6371000.0


# ---------------------------------------------------------------------------
# 几何
# ---------------------------------------------------------------------------
def _coord(lat: Any, lon: Any) -> Tuple[float, float]:
    lat, lon = float(lat), float(lon)
    if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("坐标非法: %r,%r" % (lat, lon))
    return lat, lon


def distance(a: Sequence[float], b: Sequence[float]) -> float:
    lat1, lon1 = map(math.radians, _coord(*a))
    lat2, lon2 = map(math.radians, _coord(*b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return EARTH_R * 2 * math.asin(min(1.0, math.sqrt(max(0.0, h))))


def interpolate(a: Sequence[float], b: Sequence[float], frac: float) -> Tuple[float, float]:
    if frac <= 0:
        return float(a[0]), float(a[1])
    if frac >= 1:
        return float(b[0]), float(b[1])
    ang = distance(a, b) / EARTH_R
    if ang < 1e-12:
        return float(a[0]), float(a[1])
    p = []
    for lat, lon in (a, b):
        la, lo = math.radians(lat), math.radians(lon)
        p.append((math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la)))
    wa = math.sin((1 - frac) * ang) / math.sin(ang)
    wb = math.sin(frac * ang) / math.sin(ang)
    x, y, z = (wa * p[0][i] + wb * p[1][i] for i in range(3))
    return math.degrees(math.atan2(z, math.hypot(x, y))), math.degrees(math.atan2(y, x))


def build_route(points: Iterable[Sequence[float]]) -> List[Tuple[float, float]]:
    route: List[Tuple[float, float]] = []
    for p in points:
        c = _coord(*p)
        if not route or distance(route[-1], c) > 1e-6:
            route.append(c)
    if len(route) > 1 and distance(route[0], route[-1]) <= 1e-6:
        route.pop()
    if len(route) < 2:
        raise ValueError("路线至少需要两个不同坐标点")
    return route


def smooth_ring(points: List[Tuple[float, float]], iterations: int = 2) -> List[Tuple[float, float]]:
    """Chaikin 细分：把折线拐角磨圆，更像沿道路/跑道跑出来的线。"""
    pts = list(points)
    for _ in range(max(0, iterations)):
        out: List[Tuple[float, float]] = []
        n = len(pts)
        for i in range(n):
            p, q = pts[i], pts[(i + 1) % n]
            out.append((0.75 * p[0] + 0.25 * q[0], 0.75 * p[1] + 0.25 * q[1]))
            out.append((0.25 * p[0] + 0.75 * q[0], 0.25 * p[1] + 0.75 * q[1]))
        pts = out
    return pts


def _densify(points: List[Tuple[float, float]], max_seg: float = 8.0) -> List[Tuple[float, float]]:
    out: List[Tuple[float, float]] = []
    n = len(points)
    for i in range(n):
        a, b = points[i], points[(i + 1) % n]
        out.append(a)
        seg = distance(a, b)
        k = int(seg // max_seg)
        for j in range(1, k + 1):
            out.append(interpolate(a, b, j / (k + 1)))
    return out


def _cumulative(points: List[Tuple[float, float]]) -> List[float]:
    cum = [0.0]
    for a, b in zip(points, points[1:]):
        cum.append(cum[-1] + distance(a, b))
    if len(points) > 1:
        cum.append(cum[-1] + distance(points[-1], points[0]))   # 闭环最后一段
    return cum


def _place(points: List[Tuple[float, float]], cum: List[float], traveled: float,
           cycle: float) -> Tuple[float, float]:
    t = traveled % cycle if cycle > 0 else 0.0
    lo, hi = 0, len(cum) - 1
    while lo < hi - 1:
        mid = (lo + hi) // 2
        if cum[mid] <= t:
            lo = mid
        else:
            hi = mid
    span = cum[hi] - cum[lo]
    frac = 0.0 if span <= 1e-9 else (t - cum[lo]) / span
    a = points[lo % len(points)]
    b = points[hi % len(points)]
    return interpolate(a, b, max(0.0, min(1.0, frac)))


def _jitter(pos: Tuple[float, float], meters: float, rng: random.Random) -> Tuple[float, float]:
    if meters <= 0:
        return pos
    ang = rng.uniform(0, 2 * math.pi)
    dlat = meters * math.cos(ang) / 111000.0
    dlon = meters * math.sin(ang) / (111000.0 * max(0.2, math.cos(math.radians(pos[0]))))
    return pos[0] + dlat, pos[1] + dlon


# ---------------------------------------------------------------------------
# 采样
# ---------------------------------------------------------------------------
@dataclass
class TrackResult:
    points: List[dict]
    path: List[Tuple[float, float]]
    distance_m: float
    duration_s: int
    steps: int
    active_s: float
    pace_sec: float
    speed_mps: float
    pause_s: float = 0.0
    notes: List[str] = field(default_factory=list)

    def describe(self) -> str:
        return "%dm / %ds / %s / %.2f m/s / 步数 %d / 停顿 %.0fs" % (
            round(self.distance_m), self.duration_s, pace_text(self.pace_sec),
            self.speed_mps, self.steps, self.pause_s)


def _rng_opt(cfg: dict, key: str, default):
    val = (cfg or {}).get(key, default)
    if isinstance(val, (list, tuple)) and len(val) == 2:
        try:
            lo, hi = float(val[0]), float(val[1])
            return (lo, hi) if hi >= lo else default
        except (TypeError, ValueError):
            return default
    return default


def _sample_once(route: Sequence[Sequence[float]], target_m: float, speed_mps: float,
                 start_ms: int, cadence: float, run_cfg: Optional[dict] = None,
                 seed: Optional[int] = None) -> TrackResult:
    """按"真实 GPS 轨迹"的方式采样：随机间隔、丢点、停顿、噪声、解耦 speed。"""
    if target_m <= 0 or speed_mps <= 0:
        raise ValueError("里程与速度必须为正")
    rng = random.Random(seed if seed is not None else int(time.time() * 1000) & 0xFFFFFFFF)
    cfg = run_cfg or {}

    interval = _rng_opt(cfg, "sample_interval_s", (0.9, 1.6))
    gap_prob = float(cfg.get("gps_gap_prob", 0.08))
    gap_s = _rng_opt(cfg, "gps_gap_s", (3.0, 9.0))
    noise_r = _rng_opt(cfg, "gps_noise_m", (1.5, 6.0))
    acc_r = _rng_opt(cfg, "accuracy_range", (3.0, 45.0))
    pause_prob = float(cfg.get("pause_prob", 0.75))
    pause_s = _rng_opt(cfg, "pause_sec", (8.0, 45.0))
    speed_noise = float(cfg.get("speed_noise", 0.07))

    ring = _densify(smooth_ring(build_route(route), iterations=int(cfg.get("path_smooth", 2))),
                    max_seg=float(cfg.get("path_seg_m", 8.0)))
    cum = _cumulative(ring)
    cycle = cum[-1]

    # 停顿计划（按里程比例 15%–85% 之间随机放 0–3 次）
    n_pause = 0 if rng.random() > pause_prob else rng.randint(1, 3)
    pauses = sorted(rng.uniform(0.15, 0.85) for _ in range(n_pause))

    phase1, phase2 = rng.uniform(0, 2 * math.pi), rng.uniform(0, 2 * math.pi)
    k1, k2 = rng.uniform(0.8, 1.6), rng.uniform(2.4, 3.6)

    pts: List[dict] = []
    elapsed = 0.0
    traveled = 0.0
    pause_total = 0.0
    pause_idx = 0

    def accuracy() -> float:
        if rng.random() < 0.03:
            return round(rng.uniform(max(20.0, acc_r[1] * 0.5), acc_r[1]), 1)
        return round(max(acc_r[0], min(acc_r[1] * 0.45, rng.gauss(6.5, 2.6))), 1)

    pts.append({"lat": round(ring[0][0], 7), "lon": round(ring[0][1], 7),
                "time": int(start_ms), "speed": 0.0, "accuracy": accuracy(), "isValid": True})

    guard = 0
    while traveled < target_m - 1e-6:
        guard += 1
        if guard > 20000:
            raise ValueError("采样点过多，检查参数")
        dt = rng.uniform(*interval)
        if rng.random() < gap_prob:
            dt += rng.uniform(*gap_s)
        u = traveled / target_m
        base = speed_mps * (1 + 0.10 * math.sin(2 * math.pi * k1 * u + phase1)
                            + 0.05 * math.sin(2 * math.pi * k2 * u + phase2))
        v = max(0.5, base * (1 + rng.gauss(0, speed_noise * 0.6)))
        step = v * dt
        traveled = min(target_m, traveled + step)
        elapsed += dt

        pos = _place(ring, cum, traveled, cycle)
        bulge = noise_r[0] + (noise_r[1] - noise_r[0]) * (0.5 + 0.5 * math.sin(2 * math.pi * 0.7 * u + phase1))
        pos = _jitter(pos, max(0.0, bulge * 0.35 + rng.gauss(0, 0.35)), rng)
        seg = distance((pts[-1]["lat"], pts[-1]["lon"]), pos) if traveled > 0 else step
        field = seg / dt if dt > 0 else v
        field = max(0.0, min(6.0, field * (1 + rng.gauss(0, speed_noise))))
        pts.append({"lat": round(pos[0], 7), "lon": round(pos[1], 7),
                    "time": int(start_ms + elapsed * 1000), "speed": round(field, 2),
                    "accuracy": accuracy(), "isValid": True})

        # 停顿：原地多个点，speed≈0，不产生距离
        if pause_idx < len(pauses) and u >= pauses[pause_idx] and traveled < target_m * 0.96:
            dur = rng.uniform(*pause_s)
            end_t = elapsed + dur
            while elapsed < end_t - 1e-6:
                dd = min(end_t - elapsed, rng.uniform(2.0, 4.0))
                elapsed += dd
                pts.append({"lat": pts[-1]["lat"], "lon": pts[-1]["lon"],
                            "time": int(start_ms + elapsed * 1000),
                            "speed": round(max(0.0, rng.gauss(0.05, 0.05)), 2),
                            "accuracy": accuracy(), "isValid": True})
            pause_total += dur
            pause_idx += 1

    # 收尾：末点精确落在目标距离处，时间取整
    duration = int(round(elapsed))
    pts[-1]["time"] = int(start_ms + duration * 1000)
    for i in range(1, len(pts)):
        if pts[i]["time"] <= pts[i - 1]["time"]:
            pts[i]["time"] = pts[i - 1]["time"] + 1

    actual = sum(distance((a["lat"], a["lon"]), (b["lat"], b["lon"])) for a, b in zip(pts, pts[1:]))
    active = max(1.0, duration - pause_total)
    steps = int(round(active * cadence / 60.0))
    pace = duration / (actual / 1000.0) if actual > 0 else 0
    return TrackResult(points=pts, path=ring, distance_m=actual, duration_s=duration,
                       steps=max(1, steps), active_s=active, pace_sec=pace,
                       speed_mps=actual / max(1, duration), pause_s=pause_total,
                       notes=["停顿 %d 次共 %.0fs" % (n_pause, pause_total)] if n_pause else [])


def sample_track(route: Sequence[Sequence[float]], target_m: float, speed_mps: float,
                 start_ms: int, cadence: float, run_cfg: Optional[dict] = None,
                 seed: Optional[int] = None, tolerance: float = 0.03) -> TrackResult:
    """采样并在"噪声导致的里程偏差"上做补偿，保证实际里程贴近目标。"""
    rng = random.Random(seed if seed is not None else int(time.time() * 1000) & 0xFFFFFFFF)
    target = float(target_m)
    best: Optional[TrackResult] = None
    for _ in range(5):
        tr = _sample_once(route, target, speed_mps, start_ms, cadence, run_cfg,
                          seed=rng.randint(1, 10 ** 9))
        err = (tr.distance_m - target_m) / max(1.0, target_m)
        if best is None or abs(err) < abs((best.distance_m - target_m) / max(1.0, target_m)):
            best = tr
        if abs(err) <= tolerance:
            return tr
        target *= target_m / max(1.0, tr.distance_m)
    return best


# ---------------------------------------------------------------------------
# 分桶（真实波动）
# ---------------------------------------------------------------------------
def buckets(points: List[dict], duration_s: int, steps: int, start_ms: int,
            distance_m: float, rng: Optional[random.Random] = None) -> Tuple[List[dict], List[dict]]:
    """按"实际分段长度 × 时间重叠"统计距离；步数按移动时间 + 抖动分配。"""
    rng = rng or random.Random()
    total_ms = max(1, duration_s * 1000)
    n = int(math.ceil(total_ms / 10000.0))

    dist_bins = [0.0] * n
    move_bins = [0.0] * n
    for a, b in zip(points, points[1:]):
        seg = distance((a["lat"], a["lon"]), (b["lat"], b["lon"]))
        t0 = a["time"] - start_ms
        t1 = b["time"] - start_ms
        if t1 <= t0:
            continue
        if seg > 0.001:
            # 把这一段长度按时间重叠摊到各桶
            i0, i1 = int(t0 // 10000), min(n - 1, int((t1 - 1) // 10000))
            for i in range(max(0, i0), i1 + 1):
                lo = max(t0, i * 10000)
                hi = min(t1, (i + 1) * 10000)
                if hi > lo:
                    share = (hi - lo) / (t1 - t0)
                    dist_bins[i] += seg * share
                    move_bins[i] += (hi - lo)

    # 距离：整体缩放到与 totalDis 一致（误差来自浮点/末点对齐）
    tot = sum(dist_bins)
    if tot > 0:
        k = distance_m / tot
        dist_bins = [d * k for d in dist_bins]

    move_total = sum(move_bins) or 1.0
    raw = [max(0.0, steps * m / move_total * rng.uniform(0.86, 1.14)) for m in move_bins]
    ints = [int(round(x)) for x in raw]
    residual = steps - sum(ints)
    order = sorted(range(n), key=lambda i: raw[i], reverse=True)
    j = 0
    while residual != 0 and order:
        i = order[j % len(order)]
        if residual > 0:
            ints[i] += 1
            residual -= 1
        else:
            if ints[i] > 0 and move_bins[i] > 0:
                ints[i] -= 1
                residual += 1
        j += 1
        if j > 4 * n:
            break

    speed_b, step_b = [], []
    for i in range(n):
        begin = i * 10000
        end = min(begin + 10000, total_ms)
        speed_b.append({"distance": round(dist_bins[i], 2), "beginTime": int(start_ms + begin),
                        "endTime": int(start_ms + end), "flag": 0, "state": 0, "queueNum": i + 1})
        step_b.append({"stepsNum": int(ints[i]), "beginTime": int(start_ms + begin),
                       "endTime": int(start_ms + end), "flag": 0,
                       "maxDiff": round(rng.uniform(0.01, 0.12), 3),
                       "minDiff": round(rng.uniform(0.0, 0.04), 3),
                       "avgDiff": round(rng.uniform(0.02, 0.08), 3),
                       "state": 0, "queueNum": i + 1})
    return speed_b, step_b


# ---------------------------------------------------------------------------
# 报文
# ---------------------------------------------------------------------------
def gzip_b64(text: str) -> str:
    return base64.b64encode(gzip.compress(text.encode("utf-8"), compresslevel=6)).decode("ascii")


def build_record(
    *,
    uid: int,
    unid: int,
    run_uuid: str,
    sport_type: int,
    distance_m: int,
    duration_s: int,
    steps: int,
    start_ms: int,
    stop_ms: int,
    gps: List[dict],
    checkpoints: List[dict],
    route_vertices: List[Tuple[float, float]],
    sel_distance: int,
    sel_runtime: int,
    policy: int = 1,
    run_area_id: int = -1,
    fences_raw: Optional[list] = None,
    calorie: Optional[int] = None,
    avg_power: Optional[int] = None,
    seed: Optional[int] = None,
) -> Tuple[dict, dict, str]:
    avg_step = round(steps * 60.0 / duration_s) if duration_s else 165
    speed_val = round(distance_m / duration_s * 1000) if duration_s else 2600
    calorie = calorie if calorie is not None else max(1, int(distance_m / 1000.0 * 62))
    avg_power = avg_power if avg_power is not None else int(120 + distance_m / 1000.0 * 12)
    complete = distance_m >= sel_distance and duration_s >= sel_runtime
    reason = 0 if complete else 10

    sign_map = {
        "address": "",
        "avgPower": avg_power,
        "avgStepFreq": avg_step,
        "calorie": calorie,
        "complete": complete,
        "errorCode": 200,
        "faceCheck": 0,
        "geeToken": "",
        "getPrize": False,
        "goalId": 0,
        "policy": int(policy),
        "selDistance": int(sel_distance),
        "selRunTime": int(sel_runtime),
        "selectedUnid": int(unid),
        "speed": speed_val,
        "sportType": int(sport_type),
        "startTime": int(start_ms),
        "status": 1,
        "stopTime": int(stop_ms),
        "themeId": -1,
        "totalAscent": 0,
        "totalDis": int(distance_m),
        "totalSteps": int(steps),
        "totalTime": int(duration_s),
        "uid": int(uid),
        "unCompleteReason": reason,
        "unauthorized": 0,
        "useMobilityTools": 0,
        "uuid": run_uuid,
        "validDis": int(distance_m),
        "validTime": int(duration_s),
    }

    speed_b, step_b = buckets(gps, duration_s, steps, start_ms, distance_m,
                              random.Random(seed if seed is not None else int(time.time() * 1000)))
    all_loc = json.dumps(gps, ensure_ascii=False, separators=(",", ":"))
    five_raw = json.dumps([_checkpoint_payload(c) for c in checkpoints],
                          ensure_ascii=False, separators=(",", ":")) if checkpoints else "[]"
    if sport_type == 1:
        envelope = {"useZip": False, "fivePointJson": five_raw,
                    "runAreaId": int(run_area_id), "freedomShowFence": False}
        if fences_raw:
            envelope["geoFencesJson"] = json.dumps(fences_raw, ensure_ascii=False, separators=(",", ":"))
        five_json = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    else:
        five_json = "[]"

    speed_json = json.dumps(speed_b, separators=(",", ":"))
    step_json = json.dumps(step_b, separators=(",", ":"))

    body = {
        "address": "",
        "allLocJson": all_loc,
        "avgPower": avg_power,
        "avgStepFreq": avg_step,
        "calorie": calorie,
        "complete": complete,
        "errorCode": 200,
        "faceCheck": 0,
        "fivePointJson": five_json,
        "geeToken": "",
        "getPrize": False,
        "goalId": 0,
        "isUpload": False,
        "latitude": gps[0]["lat"],
        "longitude": gps[0]["lon"],
        "maxRunTime": 0,
        "minSteps": 0,
        "originalSign": protocol.canonical(sign_map),
        "policy": int(policy),
        "recordUrl": "",
        "roomId": 0,
        "segmentJson": "[]",
        "selDistance": int(sel_distance),
        "selRunTime": int(sel_runtime),
        "selectedUnid": int(unid),
        "signature": protocol.sport_signature(sign_map),
        "speed": speed_val,
        "speedPerTenSec": speed_b,
        "sportType": int(sport_type),
        "startTime": int(start_ms),
        "status": 1,
        "stepsPerTenSec": step_b,
        "stopTime": int(stop_ms),
        "themeId": -1,
        "totalAscent": 0,
        "totalDis": int(distance_m),
        "totalSteps": int(steps),
        "totalTime": int(duration_s),
        "uid": int(uid),
        "unCompleteReason": reason,
        "unauthorized": 0,
        "useMobilityTools": 0,
        "uuid": run_uuid,
        "validDis": int(distance_m),
        "validTime": int(duration_s),
    }

    obs_payload = {
        "rrid": gzip_b64(""),
        "uid": gzip_b64(str(uid)),
        "uuid": gzip_b64(run_uuid),
        "run_data": gzip_b64(all_loc),
        "step_freq_json": gzip_b64(step_json),
        "speed_json": gzip_b64(speed_json),
        "segment_json": gzip_b64("[]"),
        "fixed_point_json": gzip_b64(five_json),
        "runFaceCheck": gzip_b64(""),
        "extension_json": gzip_b64(""),
        "laps_json": gzip_b64("[]"),
    }
    hour = time.strftime("%Y%m%d%H", time.gmtime(start_ms / 1000.0 + 8 * 3600))
    object_key = "run_data/%s/%s.json" % (hour, run_uuid)
    return body, obs_payload, object_key


def _checkpoint_payload(cp: dict) -> dict:
    out = {k: v for k, v in cp.items() if k not in ("glat", "glon", "glng")}
    out.setdefault("isPass", True)
    out.setdefault("isFixed", 1)
    return out


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------
class TrackCheck(Exception):
    pass


def verify_track(gps: List[dict], distance_m: float, duration_s: int, start_ms: int,
                 checkpoints: List[dict], tolerance_m: float = 30.0) -> dict:
    if len(gps) < 2:
        raise TrackCheck("轨迹点不足")
    if gps[0]["time"] != start_ms:
        raise TrackCheck("首点时间与 startTime 不一致")
    if abs(gps[-1]["time"] - (start_ms + duration_s * 1000)) > 1000:
        raise TrackCheck("末点时间与 stopTime 偏差过大")
    for a, b in zip(gps, gps[1:]):
        if b["time"] <= a["time"]:
            raise TrackCheck("轨迹时间不是严格递增")
    actual = sum(distance((a["lat"], a["lon"]), (b["lat"], b["lon"])) for a, b in zip(gps, gps[1:]))
    if abs(actual - distance_m) > max(3.0, distance_m * 0.005):
        raise TrackCheck("轨迹里程 %.1fm 与 totalDis %dm 偏差过大" % (actual, distance_m))
    missing = []
    for cp in checkpoints:
        target = (cp.get("lat"), cp.get("lng", cp.get("lon")))
        if not any(distance((p["lat"], p["lon"]), target) <= tolerance_m for p in gps):
            missing.append(cp.get("pointName") or cp.get("name") or target)
    pace = duration_s / (actual / 1000.0) if actual > 0 else 0
    speeds = [p["speed"] for p in gps if p["speed"] > 0]
    return {
        "points": len(gps),
        "distance_m": round(actual, 1),
        "pace_sec_per_km": round(pace, 1),
        "speed_mps": round(actual / duration_s, 3),
        "missing_checkpoints": missing,
        "speed_mean": round(sum(speeds) / len(speeds), 2) if speeds else 0,
        "speed_stdev": round((sum((s - sum(speeds) / len(speeds)) ** 2 for s in speeds) / len(speeds)) ** 0.5, 3) if speeds else 0,
    }


def pace_text(sec_per_km: float) -> str:
    m, s = divmod(int(round(sec_per_km)), 60)
    return "%d'%02d\"/km" % (m, s)


def steps_for(distance_m: float, per_km: float = 1120.0, rng: Optional[random.Random] = None) -> int:
    r = rng or random.Random()
    return int(distance_m / 1000.0 * per_km * r.uniform(0.97, 1.03))
