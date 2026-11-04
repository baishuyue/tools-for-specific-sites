# -*- coding: utf-8 -*-
"""轨迹合成与结算报文构造（v3.1：对齐真实客户端的 27 字段轨迹点）。

为什么必须对齐字段（2026-11-04 复盘）：
    真实客户端上传的轨迹点长这样（27 个字段）：
        gLat / gLng        GCJ-02 坐标（地图画绿线用的就是它）
        lat / lng          -1.0（用 gcj02 时客户端就是这么写的）
        gainTime           "YYYY-MM-DD HH:MM:SS"
        type               0 普通 / 2 打卡点 / 5 起点 / 6 终点 / 7 停顿
        radius             1.5–3.5（GPS 精度半径）
        speed / avgSpeed   瞬时速度、滑动平均
        totalDis / totalTime / validDis / validTime
        id / flag / count / state / locType / coorType / queueNum / stepDistance / bd*
    我们之前只发 {lat, lon, time, speed, accuracy, isValid}：
        → App 读不到 gLat/gLng，地图上**没有绿色轨迹**；
        → 服务端也解析不出有效轨迹，`reasonList` 出现 `运动数据异常`。

v3.1 生成策略：
    * 路径 Chaikin 平滑 + 8m 加密，采样间隔随机 0.9–1.6s、8% 概率丢点 3–9s；
    * 0–3 次真实停顿（8–45s，speed≈0，type=7）；
    * 位置低频摆动 + 小抖动，radius 1.4–3.5（偶发 4–8）；
    * speed 与几何解耦（±7%），偶发 GPS 速度尖峰；
    * 10 秒分桶按"分段长度 × 时间重叠"统计，步数按移动时间分配后校正；
    * 实际里程由几何反算，采样层迭代补偿，保证贴近目标。
"""
from __future__ import annotations

import base64
import calendar
import gzip
import json
import math
import random
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import protocol

EARTH_R = 6371000.0
TZ_OFFSET = 8 * 3600
TIME_FMT = "%Y-%m-%d %H:%M:%S"

#: 真实客户端轨迹点字段（顺序无关，但必须齐全）
CLIENT_POINT_KEYS = (
    "avgSpeed", "bdA", "bdD", "bdG", "bdS", "coorType", "count", "dtr", "flag",
    "gLat", "gLng", "gainTime", "id", "lat", "lng", "locType", "locationId",
    "queueNum", "radius", "speed", "state", "stepDistance", "totalDis",
    "totalTime", "type", "validDis", "validTime",
)


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
        cum.append(cum[-1] + distance(points[-1], points[0]))
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


def _fmt_time(ms: float) -> str:
    return time.strftime(TIME_FMT, time.gmtime(ms / 1000.0 + TZ_OFFSET))


def parse_gain_time(text: str) -> int:
    try:
        return calendar.timegm(time.strptime(text, TIME_FMT)) - TZ_OFFSET
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# 采样 → 客户端 27 字段点
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
    times: List[float] = field(default_factory=list)
    speeds: List[float] = field(default_factory=list)

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


def _to_client_points(samples: List[dict], start_ms: int, duration_s: int,
                      rng: random.Random, checkpoints: Optional[List[dict]] = None) -> List[dict]:
    """把内部采样点转成真实客户端的 27 字段结构。"""
    points: List[dict] = []
    total = 0.0
    prev: Optional[dict] = None
    base_id = rng.randint(8000, 30000)
    speed_hist: List[float] = []

    cps = []
    for c in (checkpoints or []):
        try:
            cps.append((float(c.get("lat")), float(c.get("lng", c.get("lon")))))
        except (TypeError, ValueError):
            continue

    # 每个打卡点只标记"最近的那一个采样点"，与真实客户端一致（5 个点 → 5 次 type=2）
    cp_hits = set()
    for cp in cps:
        best_i, best_d = -1, 1e9
        for i, sm in enumerate(samples):
            d = distance((sm["lat"], sm["lon"]), cp)
            if d < best_d:
                best_i, best_d = i, d
        if 0 <= best_i < len(samples) and best_d <= 60:
            cp_hits.add(best_i)

    for i, sm in enumerate(samples):
        lat, lon = sm["lat"], sm["lon"]
        if prev is not None:
            total += distance((prev["gLat"], prev["gLng"]), (lat, lon))
        v = float(sm.get("speed") or 0.0)
        speed_hist.append(v)
        window = speed_hist[-5:]
        avg_v = sum(window) / len(window)

        if i == 0:
            ptype = 5
        elif i == len(samples) - 1:
            ptype = 6
        elif sm.get("pause") or v < 0.3:
            ptype = 7
        elif i in cp_hits:
            ptype = 2
        else:
            ptype = 0

        pt = {
            "avgSpeed": round(avg_v, 2),
            "bdA": 0.0, "bdD": 0.0, "bdG": 1, "bdS": 0.0,
            "coorType": "gcj02",
            "count": 20,
            "dtr": 0.0,
            "flag": int(start_ms),
            "gLat": round(lat, 7),
            "gLng": round(lon, 7),
            "gainTime": _fmt_time(start_ms + sm["time"] * 1000.0),
            "id": base_id + i,
            "lat": -1.0,
            "lng": -1.0,
            "locType": 1,
            "locationId": "",
            "queueNum": 4 if i > len(samples) * 0.4 else i % 4,
            "radius": round(float(sm.get("radius") or 2.2), 2),
            "speed": round(v, 2),
            "state": 1,
            "stepDistance": 0.0,
            "totalDis": round(total, 3),
            "totalTime": 0,
            "type": ptype,
            "validDis": round(total, 3),
            "validTime": 0,
        }
        points.append(pt)
        prev = pt

    if points:
        points[-1]["totalTime"] = int(duration_s)
        points[-1]["validTime"] = int(duration_s)
        points[-1]["totalDis"] = round(total, 3)
        points[-1]["validDis"] = round(total, 3)
    return points


def _sample_once(route: Sequence[Sequence[float]], target_m: float, speed_mps: float,
                 start_ms: int, cadence: float, run_cfg: Optional[dict] = None,
                 seed: Optional[int] = None,
                 checkpoints: Optional[List[dict]] = None) -> TrackResult:
    if target_m <= 0 or speed_mps <= 0:
        raise ValueError("里程与速度必须为正")
    rng = random.Random(seed if seed is not None else int(time.time() * 1000) & 0xFFFFFFFF)
    cfg = run_cfg or {}

    interval = _rng_opt(cfg, "sample_interval_s", (0.9, 1.6))
    gap_prob = float(cfg.get("gps_gap_prob", 0.08))
    gap_s = _rng_opt(cfg, "gps_gap_s", (3.0, 9.0))
    noise_r = _rng_opt(cfg, "gps_noise_m", (1.5, 6.0))
    radius_r = _rng_opt(cfg, "radius_range", (1.4, 3.5))
    pause_prob = float(cfg.get("pause_prob", 0.85))
    pause_s = _rng_opt(cfg, "pause_sec", (8.0, 45.0))
    speed_noise = float(cfg.get("speed_noise", 0.07))

    ring = _densify(smooth_ring(build_route(route), iterations=int(cfg.get("path_smooth", 2))),
                    max_seg=float(cfg.get("path_seg_m", 8.0)))
    cum = _cumulative(ring)
    cycle = cum[-1]

    n_pause = 0 if rng.random() > pause_prob else rng.randint(1, 3)
    pauses = sorted(rng.uniform(0.15, 0.85) for _ in range(n_pause))

    phase1, phase2 = rng.uniform(0, 2 * math.pi), rng.uniform(0, 2 * math.pi)
    k1, k2 = rng.uniform(0.8, 1.6), rng.uniform(2.4, 3.6)

    def radius() -> float:
        if rng.random() < 0.05:
            return round(rng.uniform(4.0, 8.0), 2)
        return round(max(radius_r[0], min(radius_r[1], rng.gauss(2.2, 0.45))), 2)

    samples: List[dict] = [{"lat": ring[0][0], "lon": ring[0][1], "time": 0.0,
                            "speed": 0.0, "radius": radius(), "pause": True}]
    elapsed = 0.0
    traveled = 0.0
    pause_total = 0.0
    pause_idx = 0
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

        seg = distance((samples[-1]["lat"], samples[-1]["lon"]), pos) if traveled > 0 else step
        field = seg / dt if dt > 0 else v
        field = max(0.0, min(6.0, field * (1 + rng.gauss(0, speed_noise))))
        if rng.random() < 0.02:
            field = rng.uniform(8.0, 30.0)              # 真实 GPS 偶发速度尖峰
        samples.append({"lat": pos[0], "lon": pos[1], "time": elapsed,
                        "speed": field, "radius": radius(), "pause": False})

        if pause_idx < len(pauses) and u >= pauses[pause_idx] and traveled < target_m * 0.96:
            dur = rng.uniform(*pause_s)
            end_t = elapsed + dur
            while elapsed < end_t - 1e-6:
                dd = min(end_t - elapsed, rng.uniform(2.0, 4.0))
                elapsed += dd
                samples.append({"lat": samples[-1]["lat"], "lon": samples[-1]["lon"],
                                "time": elapsed,
                                "speed": max(0.0, rng.gauss(0.05, 0.05)),
                                "radius": radius(), "pause": True})
            pause_total += dur
            pause_idx += 1

    duration = max(1, int(round(elapsed)))
    points = _to_client_points(samples, start_ms, duration, rng, checkpoints=checkpoints)
    actual = sum(distance((a["gLat"], a["gLng"]), (b["gLat"], b["gLng"]))
                 for a, b in zip(points, points[1:]))
    active = max(1.0, duration - pause_total)
    steps = int(round(active * cadence / 60.0))
    pace = duration / (actual / 1000.0) if actual > 0 else 0
    return TrackResult(points=points, path=ring, distance_m=actual, duration_s=duration,
                       steps=max(1, steps), active_s=active, pace_sec=pace,
                       speed_mps=actual / max(1, duration), pause_s=pause_total,
                       notes=["停顿 %d 次共 %.0fs" % (n_pause, pause_total)] if n_pause else [],
                       times=[sm["time"] for sm in samples],
                       speeds=[sm["speed"] for sm in samples])


def sample_track(route: Sequence[Sequence[float]], target_m: float, speed_mps: float,
                 start_ms: int, cadence: float, run_cfg: Optional[dict] = None,
                 seed: Optional[int] = None, tolerance: float = 0.03,
                 checkpoints: Optional[List[dict]] = None) -> TrackResult:
    """采样并按噪声导致的里程偏差做补偿，保证实际里程贴近目标。"""
    rng = random.Random(seed if seed is not None else int(time.time() * 1000) & 0xFFFFFFFF)
    target = float(target_m)
    best: Optional[TrackResult] = None
    for _ in range(5):
        tr = _sample_once(route, target, speed_mps, start_ms, cadence, run_cfg,
                          seed=rng.randint(1, 10 ** 9), checkpoints=checkpoints)
        err = (tr.distance_m - target_m) / max(1.0, target_m)
        if best is None or abs(err) < abs((best.distance_m - target_m) / max(1.0, target_m)):
            best = tr
        if abs(err) <= tolerance:
            return tr
        target *= target_m / max(1.0, tr.distance_m)
    return best


# ---------------------------------------------------------------------------
# 分桶
# ---------------------------------------------------------------------------
def buckets(points: List[dict], duration_s: int, steps: int, start_ms: int,
            distance_m: float, rng: Optional[random.Random] = None) -> Tuple[List[dict], List[dict]]:
    rng = rng or random.Random()
    total_ms = max(1, duration_s * 1000)
    n = int(math.ceil(total_ms / 10000.0))

    times = [parse_gain_time(p.get("gainTime", "")) * 1000 for p in points]
    times = [t - start_ms if t else 0 for t in times]
    for i in range(1, len(times)):
        if times[i] <= times[i - 1]:
            times[i] = times[i - 1] + 1

    dist_bins = [0.0] * n
    move_bins = [0.0] * n
    for i in range(len(points) - 1):
        seg = distance((points[i]["gLat"], points[i]["gLng"]),
                       (points[i + 1]["gLat"], points[i + 1]["gLng"]))
        t0, t1 = times[i], times[i + 1]
        if t1 <= t0 or seg <= 0.001:
            continue
        i0, i1 = int(t0 // 10000), min(n - 1, int((t1 - 1) // 10000))
        for b in range(max(0, i0), i1 + 1):
            lo, hi = max(t0, b * 10000), min(t1, (b + 1) * 10000)
            if hi > lo:
                share = (hi - lo) / (t1 - t0)
                dist_bins[b] += seg * share
                move_bins[b] += (hi - lo)

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
        elif ints[i] > 0 and move_bins[i] > 0:
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
        "latitude": gps[0]["gLat"],
        "longitude": gps[0]["gLng"],
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
    hour = time.strftime("%Y%m%d%H", time.gmtime(start_ms / 1000.0 + TZ_OFFSET))
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
    if set(CLIENT_POINT_KEYS) - set(gps[0].keys()):
        raise TrackCheck("轨迹点字段缺失：%s" % sorted(set(CLIENT_POINT_KEYS) - set(gps[0].keys())))
    t0 = parse_gain_time(gps[0]["gainTime"])
    t1 = parse_gain_time(gps[-1]["gainTime"])
    if abs(t0 - start_ms / 1000.0) > 2:
        raise TrackCheck("首点 gainTime 与 startTime 不一致")
    if abs(t1 - (start_ms + duration_s * 1000) / 1000.0) > 2:
        raise TrackCheck("末点 gainTime 与 stopTime 不一致")
    prev = t0
    for p in gps[1:]:
        cur = parse_gain_time(p["gainTime"])
        if cur < prev:
            raise TrackCheck("gainTime 倒退")
        prev = cur
    actual = sum(distance((a["gLat"], a["gLng"]), (b["gLat"], b["gLng"]))
                 for a, b in zip(gps, gps[1:]))
    if abs(actual - distance_m) > max(3.0, distance_m * 0.005):
        raise TrackCheck("轨迹里程 %.1fm 与 totalDis %dm 偏差过大" % (actual, distance_m))
    missing = []
    for cp in checkpoints:
        target = (cp.get("lat"), cp.get("lng", cp.get("lon")))
        if not any(distance((p["gLat"], p["gLng"]), target) <= tolerance_m for p in gps):
            missing.append(cp.get("pointName") or cp.get("name") or target)
    pace = duration_s / (actual / 1000.0) if actual > 0 else 0
    speeds = [p["speed"] for p in gps if p["speed"] > 0]
    mean = sum(speeds) / len(speeds) if speeds else 0
    return {
        "points": len(gps),
        "distance_m": round(actual, 1),
        "pace_sec_per_km": round(pace, 1),
        "speed_mps": round(actual / duration_s, 3),
        "missing_checkpoints": missing,
        "speed_mean": round(mean, 2),
        "speed_stdev": round((sum((s - mean) ** 2 for s in speeds) / len(speeds)) ** 0.5, 3) if speeds else 0,
        "types": {t: sum(1 for p in gps if p["type"] == t) for t in (0, 2, 5, 6, 7)},
    }


def pace_text(sec_per_km: float) -> str:
    m, s = divmod(int(round(sec_per_km)), 60)
    return "%d'%02d\"/km" % (m, s)


def steps_for(distance_m: float, per_km: float = 1120.0, rng: Optional[random.Random] = None) -> int:
    r = rng or random.Random()
    return int(distance_m / 1000.0 * per_km * r.uniform(0.97, 1.03))
