# -*- coding: utf-8 -*-
"""坐标系工具：WGS-84 ↔ GCJ-02（火星坐标）与点是否在多边形内。

为什么需要：
    真实客户端上报的轨迹点里同时有两套坐标（实测同一点差 938 米）：
        glat/glon  → GCJ-02，App 地图画绿线用的就是这套
        lat/lon    → WGS-84，GPS 原始坐标
    接口 `pointsResModels` 只返回 `lat/lng`（WGS-84）。如果直接塞进 `gLat/gLng`，
    整条轨迹会平移约 900 米、落到校区外，于是地图上一条线都看不到。

本模块用于：
    1. 判断接口给的打卡点到底是 WGS 还是 GCJ（拿围栏比一下就知道）；
    2. 必要时把 WGS 转成 GCJ 再写进 gLat/gLng。
"""
from __future__ import annotations

import math
from typing import Iterable, List, Sequence, Tuple

#: 克拉索夫斯基椭球参数（GCJ-02 变换用）
_A = 6378245.0
_EE = 0.00669342162296594323


def out_of_china(lat: float, lng: float) -> bool:
    return not (72.004 <= lng <= 137.8347 and 0.8293 <= lat <= 55.8271)


def _transform_lat(x: float, y: float) -> float:
    ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * math.pi) + 320 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lng(x: float, y: float) -> float:
    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return ret


def wgs2gcj(lat: float, lng: float) -> Tuple[float, float]:
    """WGS-84 → GCJ-02。境外坐标原样返回。"""
    if out_of_china(lat, lng):
        return lat, lng
    dlat = _transform_lat(lng - 105.0, lat - 35.0)
    dlng = _transform_lng(lng - 105.0, lat - 35.0)
    radlat = lat / 180.0 * math.pi
    magic = math.sin(radlat)
    magic = 1 - _EE * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_A * (1 - _EE)) / (magic * sqrtmagic) * math.pi)
    dlng = (dlng * 180.0) / (_A / sqrtmagic * math.cos(radlat) * math.pi)
    return lat + dlat, lng + dlng


def gcj2wgs(lat: float, lng: float) -> Tuple[float, float]:
    """GCJ-02 → WGS-84（一次迭代，误差 < 2m，够用）。"""
    if out_of_china(lat, lng):
        return lat, lng
    wlat, wlng = lat, lng
    for _ in range(3):
        glat, glng = wgs2gcj(wlat, wlng)
        wlat += lat - glat
        wlng += lng - glng
    return wlat, wlng


_X_PI = math.pi * 3000.0 / 180.0


def bd09_to_gcj(bd_lat: float, bd_lng: float) -> Tuple[float, float]:
    """BD-09（百度）→ GCJ-02。实测与真实抓包残差 0.1 米。"""
    x = bd_lng - 0.0065
    y = bd_lat - 0.006
    z = math.sqrt(x * x + y * y) - 0.00002 * math.sin(y * _X_PI)
    theta = math.atan2(y, x) - 0.000003 * math.cos(x * _X_PI)
    return z * math.sin(theta), z * math.cos(theta)


def gcj_to_bd09(lat: float, lng: float) -> Tuple[float, float]:
    z = math.sqrt(lng * lng + lat * lat) + 0.00002 * math.sin(lat * _X_PI)
    theta = math.atan2(lat, lng) + 0.000003 * math.cos(lng * _X_PI)
    return z * math.sin(theta) + 0.006, z * math.cos(theta) + 0.0065


def polygon(points: Iterable[dict], prefer: Sequence[str] = ("glat", "glon")) -> List[Tuple[float, float]]:
    """从围栏 points 里取多边形（优先 GCJ 字段）。"""
    poly: List[Tuple[float, float]] = []
    for p in points or []:
        lat = p.get(prefer[0])
        lon = p.get(prefer[1])
        if lat in (None, -1) or lon in (None, -1):
            lat = p.get("lat", p.get("latitude"))
            lon = p.get("lon", p.get("lng", p.get("longitude")))
        try:
            lat, lon = float(lat), float(lon)
        except (TypeError, ValueError):
            continue
        if abs(lat) > 0.001 and abs(lon) > 0.001:
            poly.append((lat, lon))
    return poly


def point_in_polygon(lat: float, lng: float, poly: List[Tuple[float, float]]) -> bool:
    if len(poly) < 3:
        return False
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        yi, xi = poly[i]
        yj, xj = poly[j]
        if ((xi > lng) != (xj > lng)) and (lat < (yj - yi) * (lng - xi) / (xj - xi + 1e-12) + yi):
            inside = not inside
        j = i
    return inside


def inside_ratio(points: Iterable[dict], poly: List[Tuple[float, float]],
                 convert: bool = False) -> float:
    pts = list(points or [])
    if not pts or len(poly) < 3:
        return 0.0
    hit = 0
    for p in pts:
        try:
            lat = float(p.get("lat", p.get("glat")))
            lon = float(p.get("lng", p.get("lon", p.get("glon"))))
        except (TypeError, ValueError):
            continue
        if convert:
            lat, lon = wgs2gcj(lat, lon)
        if point_in_polygon(lat, lon, poly):
            hit += 1
    return hit / max(1, len(pts))


def _to_gcj(obj: dict, mode: str) -> Tuple[float, float]:
    lat = float(obj.get("lat", obj.get("glat")))
    lon = float(obj.get("lng", obj.get("lon", obj.get("glon"))))
    if mode == "bd09":
        return bd09_to_gcj(lat, lon)
    if mode == "wgs":
        return wgs2gcj(lat, lon)
    return lat, lon


def inside_ratio_mode(points: List[dict], poly: List[Tuple[float, float]], mode: str) -> float:
    if not points or len(poly) < 3:
        return 0.0
    hit = 0
    for p in points:
        try:
            lat, lon = _to_gcj(p, mode)
        except (TypeError, ValueError):
            continue
        if point_in_polygon(lat, lon, poly):
            hit += 1
    return hit / max(1, len(points))


def detect(points: List[dict], poly: List[Tuple[float, float]]) -> str:
    """判断接口下发的是哪种坐标：'gcj' / 'wgs' / 'bd09' / 'unknown'。

    真实抓包实测：接口点的 lat/lon 是 BD-09，围栏的 glat/glon 是 GCJ-02。
    """
    if len(poly) < 3 or not points:
        return "unknown"
    scores = {m: inside_ratio_mode(points, poly, m) for m in ("gcj", "wgs", "bd09")}
    best = max(scores, key=scores.get)
    if scores[best] >= 0.6:
        return best
    return "unknown"


def convert_points(points: List[dict], mode: str) -> List[dict]:
    """把点列表转成 GCJ（返回新 dict，保留其它字段）。"""
    out = []
    for p in points or []:
        q = dict(p)
        try:
            lat, lon = _to_gcj(p, mode)
            q["lat"], q["lng"] = lat, lon
        except (TypeError, ValueError):
            pass
        out.append(q)
    return out
