# -*- coding: utf-8 -*-
"""学校规则解析 + 合规随机构造（里程 / 配速 / 步频每次不同但都合法）。

判据来源：`/api/v70103/runModePolicy` 的 `runRuleModel`，实测字段：
    minDistance / minRunTime / dayGoal / speedTop / speedBottom
    stepTop / stepBottom / validTime / timeInterval / runningPauseTime
各校配置不同，这里统一成"区间"，再与用户偏好区间取交集后随机取值。

要点：
  * speedTop/speedBottom 两个字段谁大谁小不固定（实测 2.0 / 10.0），一律按
    "无序区间 + 取 min/max" 处理，单位按 m/s 解释（与记录里的 speed 字段一致）；
  * 配速、步频、里程三者必须互相自洽：步幅 = 里程/步数 要落在人类区间；
  * 任何一项越界都会在本地直接抛错，不发脏包。
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# 人类合理区间（用于兜底与自洽校验，不是学校规则）
HUMAN_PACE_SEC = (170, 600)        # 2'50" ~ 10'00" /km
HUMAN_CADENCE = (150, 200)         # 步/分
HUMAN_STRIDE_M = (0.55, 1.75)      # 步幅


class PlanError(RuntimeError):
    pass


def pace_text(sec_per_km: float) -> str:
    m, s = divmod(int(round(sec_per_km)), 60)
    return "%d'%02d\"/km" % (m, s)


@dataclass
class Limits:
    min_distance: int = 0
    min_run_time: int = 0
    max_run_time: int = 7200
    day_goal: int = 0
    speed_min: float = 1.5          # m/s（慢端）
    speed_max: float = 12.0         # m/s（快端）
    cadence_min: int = 60
    cadence_max: int = 300
    valid_times: List[Tuple[str, str]] = field(default_factory=list)
    policy: int = 1
    raw: Dict[str, Any] = field(default_factory=dict)

    # -- 派生 ----------------------------------------------------------
    @property
    def pace_min(self) -> float:
        """最快配速（秒/公里），由速度上限换算。"""
        return 1000.0 / self.speed_max if self.speed_max > 0 else HUMAN_PACE_SEC[0]

    @property
    def pace_max(self) -> float:
        """最慢配速（秒/公里），由速度下限换算。"""
        return 1000.0 / self.speed_min if self.speed_min > 0 else HUMAN_PACE_SEC[1]

    def describe(self) -> str:
        return ("单次≥%dm，时长≥%ds，速度 %.2f–%.2f m/s（配速 %s–%s），"
                "步频 %d–%d 步/分，日目标 %dm，时段 %s" % (
                    self.min_distance, self.min_run_time, self.speed_min, self.speed_max,
                    pace_text(self.pace_max), pace_text(self.pace_min),
                    self.cadence_min, self.cadence_max, self.day_goal,
                    ",".join("%s-%s" % t for t in self.valid_times) or "不限"))

    @classmethod
    def from_policy(cls, data: Optional[dict], run_mode: int = 1) -> "Limits":
        data = data or {}
        rule = data.get("runRuleModel") or {}

        def num(key: str, default: float = 0) -> float:
            try:
                return float(rule.get(key))
            except (TypeError, ValueError):
                return default

        s_top, s_bot = num("speedTop"), num("speedBottom")
        if s_top > 0 and s_bot > 0:
            speed_min, speed_max = sorted((s_top, s_bot))
        elif s_top > 0 or s_bot > 0:
            speed_min = speed_max = max(s_top, s_bot)
        else:
            speed_min, speed_max = 1.5, 12.0

        step_top, step_bot = num("stepTop"), num("stepBottom")
        if step_top > 0 and step_bot > 0:
            cadence_min, cadence_max = int(sorted((step_top, step_bot))[0]), int(sorted((step_top, step_bot))[1])
        else:
            cadence_min, cadence_max = 60, 300

        times: List[Tuple[str, str]] = []
        for vt in rule.get("validTime") or []:
            start, end = vt.get("start"), vt.get("end")
            if start and end:
                times.append((start, end))

        return cls(
            min_distance=int(num("minDistance")),
            min_run_time=int(num("minRunTime")),
            max_run_time=int(num("maxRunTime") or 7200),
            day_goal=int(num("dayGoal")),
            speed_min=max(0.5, speed_min),
            speed_max=max(speed_min + 0.1, speed_max),
            cadence_min=cadence_min,
            cadence_max=cadence_max,
            valid_times=times,
            policy=int(data.get("policy") or 1),
            raw=rule,
        )


def _intersect(lo: float, hi: float, other: Tuple[float, float]) -> Optional[Tuple[float, float]]:
    a, b = max(lo, other[0]), min(hi, other[1])
    return (a, b) if b > a else None


@dataclass
class RunPlan:
    distance_m: int
    duration_s: int
    steps: int
    pace_sec: float
    cadence: float
    speed_mps: float
    stride_m: float
    notes: List[str] = field(default_factory=list)

    def describe(self) -> str:
        return ("%dm / %ds / %s / 速度 %.2f m/s / 步频 %.0f 步分 / 步数 %d / 步幅 %.2fm" % (
            self.distance_m, self.duration_s, pace_text(self.pace_sec),
            self.speed_mps, self.cadence, self.steps, self.stride_m))


def _range(cfg: dict, key: str, fallback: Tuple[float, float]) -> Tuple[float, float]:
    val = cfg.get(key)
    try:
        if isinstance(val, (list, tuple)) and len(val) == 2:
            lo, hi = float(val[0]), float(val[1])
            if hi >= lo > 0:
                return lo, hi
    except (TypeError, ValueError):
        pass
    return fallback


def choose_run(limits: Limits, run_cfg: dict, rng: Optional[random.Random] = None) -> RunPlan:
    """在学校规则内随机抽一组互相自洽的里程/时长/步频。

    顺序：先定"速度窗口"（学校 ∩ 用户配速），再定时长（≥ minRunTime），
    里程由 速度×时长 反推（保证三者永远自洽），最后按步幅约束反推步频。
    """
    rng = rng or random.Random()
    notes: List[str] = []

    # ---- 1. 速度窗口（m/s）：校规 ∩ 用户偏好 ∩ 人类可信 ----
    user_pace = _range(run_cfg, "pace_range_sec_per_km", HUMAN_PACE_SEC)
    human_speed = _range(run_cfg, "human_speed_mps", (1.8, 4.2))
    v_lo = max(limits.speed_min, 1000.0 / user_pace[1], human_speed[0])
    v_hi = min(limits.speed_max, 1000.0 / user_pace[0], human_speed[1])
    if v_hi <= v_lo:
        # 用户区间与学校无交集 → 退回人类合理区间再求交集
        v_lo = max(limits.speed_min, 1000.0 / HUMAN_PACE_SEC[1])
        v_hi = min(limits.speed_max, 1000.0 / HUMAN_PACE_SEC[0])
        if v_hi <= v_lo:
            raise PlanError("学校速度区间 %.2f–%.2f m/s 与人类合理配速无交集" % (
                limits.speed_min, limits.speed_max))
        notes.append("用户配速区间与学校无交集，已改用人类合理区间")
    notes.append("速度窗口：%.2f–%.2f m/s（配速 %s–%s）" % (
        v_lo, v_hi, pace_text(1000.0 / v_hi), pace_text(1000.0 / v_lo)))

    # ---- 2. 时长 ----
    fixed_t = int(run_cfg.get("duration_sec") or 0)
    fixed_d = int(run_cfg.get("distance_m") or 0)
    lo_d, hi_d = _range(run_cfg, "distance_range_m",
                        (max(limits.min_distance + 50, 2000), max(limits.min_distance + 250, 2200)))
    lo_d = max(lo_d, limits.min_distance + 10)
    hi_d = max(hi_d, lo_d)

    speed = rng.uniform(v_lo, v_hi)
    if fixed_t > 0:
        duration = max(fixed_t, limits.min_run_time)
        # 里程在「用户区间 ∩ 速度窗口」里取，避免出现 1835m 这种低于日目标的结果
        d_lo = max(lo_d, int(math.ceil(v_lo * duration)))
        d_hi = min(hi_d, int(math.floor(v_hi * duration)))
        if d_hi <= d_lo:
            d_lo = max(limits.min_distance, int(math.ceil(v_lo * duration)))
            d_hi = max(d_lo, int(math.floor(v_hi * duration)))
        distance = int(round(rng.uniform(d_lo, d_hi) / 5.0) * 5)
        speed = distance / duration
        notes.append("时长：固定 %ds → 里程在 %d–%d 内取 %dm（%.2f m/s）" % (
            duration, d_lo, d_hi, distance, speed))
    else:
        distance = fixed_d if fixed_d > 0 else int(round(rng.uniform(lo_d, hi_d) / 5.0) * 5)
        distance = max(distance, limits.min_distance)
        duration = int(round(distance / speed))
        if limits.min_run_time and duration < limits.min_run_time:
            duration = limits.min_run_time
            distance = int(round(speed * duration / 5.0) * 5)
            notes.append("时长按下限抬到 %ds（学校 minRunTime）→ 里程反推为 %dm" % (duration, distance))
        notes.append("里程：%dm（随机区间 %.0f–%.0f，学校≥%d%s）" % (
            distance, lo_d, hi_d, limits.min_distance, "，已固定" if fixed_d > 0 else ""))

    duration = max(60, min(duration, limits.max_run_time))
    if distance < limits.min_distance:
        distance = limits.min_distance
    speed = distance / duration
    if not (limits.speed_min - 1e-6 <= speed <= limits.speed_max + 1e-6):
        # 收尾再夹一次：以时长为准，把里程压回速度窗口
        clamp_v = min(max(speed, limits.speed_min), limits.speed_max)
        distance = int(round(clamp_v * duration / 5.0) * 5)
        if distance < limits.min_distance:
            raise PlanError("在 %ds 时长下无法同时满足里程≥%dm 与速度 %.2f–%.2f m/s" % (
                duration, limits.min_distance, limits.speed_min, limits.speed_max))
        speed = distance / duration
        notes.append("速度越界，里程已夹回 %dm" % distance)
    pace = 1000.0 / speed if speed > 0 else 0
    notes.append("配速：%s（%.2f m/s，时长 %ds）" % (pace_text(pace), speed, duration))

    # ---- 3. 步频 / 步数（由步幅约束反解）----
    user_cad = _range(run_cfg, "cadence_range_spm", HUMAN_CADENCE)
    human_cad = _range(run_cfg, "human_cadence_spm", (150, 190))
    hard_lo, hard_hi = max(60, limits.cadence_min), max(61, limits.cadence_max)
    cad_lo = max(user_cad[0], hard_lo, human_cad[0])
    cad_hi = min(user_cad[1], hard_hi, human_cad[1])
    if cad_hi <= cad_lo:
        cad_lo, cad_hi = max(hard_lo, human_cad[0]), min(hard_hi, human_cad[1])
    if cad_hi <= cad_lo:
        cad_lo, cad_hi = hard_lo, hard_hi
        notes.append("用户步频与学校无交集，改用学校区间")
    cadence = rng.uniform(cad_lo, cad_hi)

    def steps_for(cad: float) -> int:
        return max(1, int(round(duration * cad / 60.0)))

    stride = distance / steps_for(cadence)
    stride_lo, stride_hi = _range(run_cfg, "human_stride_m", HUMAN_STRIDE_M)
    if not (stride_lo <= stride <= stride_hi):
        # 步幅不合理：在允许区间里找一个能让步幅落回人类区间的步频
        need_cad = 60.0 * speed / 0.72          # 以 0.72m 步幅为目标
        cand = min(max(need_cad, hard_lo), hard_hi)
        cand_stride = distance / steps_for(cand)
        if stride_lo <= cand_stride <= stride_hi:
            cadence = cand
            stride = cand_stride
            notes.append("步幅修正：步频改为 %.0f（步幅 %.2fm）" % (cadence, stride))
        else:
            raise PlanError("步幅 %.2fm 无法落在 %.2f–%.2fm（速度 %.2f m/s、步频 %d–%d）" % (
                stride, stride_lo, stride_hi, speed, hard_lo, hard_hi))
    steps = steps_for(cadence)
    stride = distance / steps
    notes.append("步频：%.0f 步/分（区间 %.0f–%.0f，学校 %d–%d）→ 步数 %d，步幅 %.2fm" % (
        cadence, cad_lo, cad_hi, limits.cadence_min, limits.cadence_max, steps, stride))

    return RunPlan(distance_m=distance, duration_s=duration, steps=steps, pace_sec=pace,
                   cadence=cadence, speed_mps=speed, stride_m=stride, notes=notes)


def validate(plan: RunPlan, limits: Limits) -> List[str]:
    """最终硬校验，返回问题列表（空 = 合规）。"""
    bad: List[str] = []
    if plan.distance_m < limits.min_distance:
        bad.append("里程 %dm < 学校下限 %dm" % (plan.distance_m, limits.min_distance))
    if plan.duration_s < limits.min_run_time:
        bad.append("时长 %ds < 学校下限 %ds" % (plan.duration_s, limits.min_run_time))
    if not (limits.speed_min - 1e-6 <= plan.speed_mps <= limits.speed_max + 1e-6):
        bad.append("速度 %.2f m/s 越界（%.2f–%.2f）" % (
            plan.speed_mps, limits.speed_min, limits.speed_max))
    if not (limits.cadence_min - 0.5 <= plan.cadence <= limits.cadence_max + 0.5):
        bad.append("步频 %.0f 越界（%d–%d）" % (plan.cadence, limits.cadence_min, limits.cadence_max))
    if not (HUMAN_STRIDE_M[0] <= plan.stride_m <= HUMAN_STRIDE_M[1]):
        bad.append("步幅 %.2fm 不合理" % plan.stride_m)
    if plan.speed_mps > 4.5:
        bad.append("速度 %.2f m/s 超过人类可信上限（4.5 m/s ≈ 13km/h）" % plan.speed_mps)
    if plan.cadence > 200:
        bad.append("步频 %.0f 超过人类可信上限（200 步/分）" % plan.cadence)
    if plan.duration_s > limits.max_run_time:
        bad.append("时长 %ds 超过上限 %ds" % (plan.duration_s, limits.max_run_time))
    return bad
