# -*- coding: utf-8 -*-
"""一键跑步编排：时间对齐 → 规则/围栏 → 打卡点 → 合成轨迹 → 上传 → 结算 → 回查。

设计原则：
  * 每一阶段都可单独中止并给出可执行的下一步（缺 token / 未到时段 / 今天已跑 / 核验中）；
  * 绝不重复上报：本地幂等锁 + 当日额度双重判断；
  * 任何本地一致性校验失败都不发请求，避免给账号留下"脏包"记录。
"""
from __future__ import annotations

import datetime
import json
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from . import geo, policy, store, track
from .client import SportClient, SportError, as_list


class AuthRequired(RuntimeError):
    pass


@dataclass
class Report:
    ok: bool
    status: str                       # done / already / pending / failed / dry
    message: str = ""
    rrid: Optional[Any] = None
    uuid: str = ""
    distance_m: float = 0.0
    duration_s: int = 0
    steps: int = 0
    pace: str = ""
    checkpoints: int = 0
    warnings: List[str] = field(default_factory=list)
    detail: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 规则解析
# ---------------------------------------------------------------------------
def parse_rule(policy_data: dict) -> dict:
    rule = (policy_data or {}).get("runRuleModel") or {}
    times = []
    for vt in rule.get("validTime") or []:
        start, end = vt.get("start"), vt.get("end")
        if start and end:
            times.append((start, end))
    return {
        "min_distance": int(rule.get("minDistance") or 0),
        "min_run_time": int(rule.get("minRunTime") or 0),
        "day_goal": int(rule.get("dayGoal") or 0),
        "max_run_time": int(rule.get("maxRunTime") or 7200),
        "speed_top": rule.get("speedTop"),
        "speed_bottom": rule.get("speedBottom"),
        "step_top": rule.get("stepTop"),
        "step_bottom": rule.get("stepBottom"),
        "valid_times": times,
        "policy": int((policy_data or {}).get("policy") or 1),
        "raw": rule,
    }


def in_valid_window(times: List[tuple], now: Optional[datetime.datetime] = None) -> bool:
    if not times:
        return True
    now = now or store.now_server()      # 服务端按 UTC+8 判定，与本机时区无关
    cur = now.strftime("%H:%M")
    for start, end in times:
        if start <= cur <= end:
            return True
        if start > end and (cur >= start or cur <= end):   # 跨零点
            return True
    return False


def pick_area(policy_data: dict) -> int:
    areas = (policy_data or {}).get("runAreaModels") or []
    valid = [a for a in areas if a.get("valid")]
    selected = [a for a in valid if a.get("select")]
    if len(selected) == 1:
        return int(selected[0].get("id") or -1)
    if len(valid) == 1:
        return int(valid[0].get("id") or -1)
    return -1


def anchor_of(fences: List[dict], area_id: int) -> Optional[tuple]:
    for fence in fences or []:
        pts = fence.get("points") or []
        if isinstance(pts, str):
            try:
                pts = json.loads(pts)
            except Exception:
                pts = []
        if not pts:
            continue
        first = pts[0]
        lat = first.get("glat", first.get("lat"))
        lon = first.get("glon", first.get("glng", first.get("lon")))
        if lat in (None, -1) or lon in (None, -1):
            continue
        return float(lat), float(lon)
    return None


def fence_ring(fences: List[dict], max_points: int = 24) -> List[tuple]:
    for fence in fences or []:
        pts = fence.get("points") or []
        if isinstance(pts, str):
            try:
                pts = json.loads(pts)
            except Exception:
                pts = []
        ring = []
        for p in pts:
            lat = p.get("glat", p.get("lat"))
            lon = p.get("glon", p.get("glng", p.get("lon")))
            if lat not in (None, -1) and lon not in (None, -1):
                ring.append((float(lat), float(lon)))
        if len(ring) >= 3:
            step = max(1, len(ring) // max_points)
            return ring[::step]
    return []


def circle_ring(lat: float, lon: float, radius_m: float = 260.0, n: int = 16) -> List[tuple]:
    dlat = radius_m / 111000.0
    dlon = radius_m / (111000.0 * max(0.2, math.cos(math.radians(lat))))
    return [(lat + dlat * math.cos(2 * math.pi * i / n),
             lon + dlon * math.sin(2 * math.pi * i / n)) for i in range(n)]


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------
def verify_now(cfg: dict, log: Callable[[str], None] = print,
               account: Optional[str] = None) -> dict:
    """不跑步，只去服务端核对：今日记录 / 最近一次提交 / 学期里程。"""
    cred = store.load_credentials(account)
    if not cred.get("token"):
        return {}
    net = cfg.get("net") or {}
    client = SportClient(uid=cred.get("uid"), token=cred.get("token"), unid=cred.get("unid"),
                         device=store.device_for(cfg, cred), timeout=float(net.get("timeout", 15)),
                         retries=2, proxy=net.get("proxy") or None,
                         insecure=bool(net.get("insecure", False)), log=log)
    try:
        client.sync_time()
        state = store.last_submit(account)
        data = client.verify_submission(state.get("uuid", ""))
        # 切号后本地可能残留上一个账号的 last_submit：本账号记录里查不到就不展示
        if state.get("uuid") and not data.get("record"):
            try:
                recent = client.records(page=1, size=10)
                if not any(r.get("uuid") == state.get("uuid") for r in recent):
                    data["stale_submit"] = state.get("uuid")
                    state = {}
                    data["last_submit"] = {}
            except Exception:
                pass
    except Exception as exc:
        return {"error": str(exc)}
    data["last_submit"] = state
    data["account_key"] = store.state_account(account)
    return data


def format_verify(v: dict, last: Optional[dict] = None) -> list:
    """把核对结果整理成人类可读的多行文本（CLI 与 GUI 共用）。"""
    lines: list = []
    rec = v.get("record") or {}
    detail = v.get("detail") or {}
    today = v.get("today") or []
    sem = v.get("semester") or {}
    last = last if last is not None else (v.get("last_submit") or {})

    if today:
        ok_recs = [r for r in today if r.get("complete")]
        total = sum(float(r.get("validDis") or 0) for r in ok_recs) / 1000.0
        lines.append("今日：%s，%d 条记录，有效合计 %.2f km" % (
            "已完成" if ok_recs else "未完成", len(today), total))
    else:
        lines.append("今日：还没有跑步记录")

    if rec.get("rrid"):
        lines.append("服务端记录：rrid=%s  complete=%s  有效=%s m  用时=%s s" % (
            rec.get("rrid"), detail.get("complete"), detail.get("validDis"),
            int(float(detail.get("totalTime") or 0))))
        if detail.get("statusInfo"):
            lines.append("判定信息：%s" % detail.get("statusInfo"))
        lines.append("轨迹落地：jsonFromObs=%s（1 = 服务端已从对象存储读到轨迹）  isCheat=%s" % (
            detail.get("jsonFromObs"), detail.get("isCheat")))
    elif last.get("uuid"):
        lines.append("服务端记录：暂未查到 uuid=%s（可能还在同步）" % last.get("uuid"))
    else:
        lines.append("服务端记录：暂无本次提交")

    if last:
        up = last.get("upload") or {}
        if up:
            lines.append("上传回执：HTTP %s，%s 字节，对象 %s" % (
                up.get("status"), up.get("bytes"), last.get("object_key", "")))
        verdict = last.get("verdict") or {}
        if verdict:
            lines.append("本地判定：%s（%s）" % (
                "成功" if verdict.get("ok") else verdict.get("status"), verdict.get("statusInfo") or ""))

    if sem:
        base = (last.get("baseline") or {})
        if base.get("semesterDis") is not None:
            try:
                delta = float(sem.get("semesterDis") or 0) - float(base.get("semesterDis") or 0)
                lines.append("学期里程：%s m → %s m（本次 %+.0f m），有效 %s m" % (
                    base.get("semesterDis"), sem.get("semesterDis"), delta,
                    sem.get("semesterValidDis")))
            except Exception:
                pass
        else:
            lines.append("学期里程：%s m（有效 %s m）" % (
                sem.get("semesterDis"), sem.get("semesterValidDis")))
    if v.get("error"):
        lines.append("核对失败：%s" % v["error"])
    return lines


class Runner:
    #: 当前账号 key（uid）；用 __new__ 构造的测试实例也不会 AttributeError
    account_key: str = ""

    def __init__(self, cfg: dict, log: Callable[[str], None] = print,
                 account: Optional[str] = None) -> None:
        self.cfg = cfg
        self.log = log
        cred = store.load_credentials(account)
        if not cred.get("token"):
            raise AuthRequired("未登录：请先执行  一键跑步.bat login  （手机号 + 密码，自动过滑块）")
        self.account = cred
        self.account_key = str(cred.get("key") or cred.get("uid") or "")
        net = cfg.get("net") or {}
        self.client = SportClient(
            uid=cred.get("uid"),
            token=cred.get("token"),
            unid=cred.get("unid"),
            device=store.device_for(cfg, cred),
            timeout=float(net.get("timeout", 15)),
            retries=int(net.get("retries", 3)),
            proxy=(net.get("proxy") or None),
            insecure=bool(net.get("insecure", False)),
            log=log,
        )
        self.run_cfg = cfg.get("run") or {}
        self.report = Report(ok=False, status="init")

    # -- 账号 --------------------------------------------------------------
    def profile(self) -> dict:
        info = self.client.user_info()
        cur = store.load_credentials()
        store.save_credentials(uid=info.get("uid"), unid=info.get("unid"),
                               source=cur.get("source", "manual"),
                               phone=str(cur.get("phone") or ""),
                               name=str(info.get("name") or cur.get("name") or ""),
                               campus=str(info.get("campusName") or info.get("universityName") or cur.get("campus") or ""))
        return info

    # -- 跑步参数 ----------------------------------------------------------
    def resolve_plan(self, policy_data: dict, fences: List[dict], run_uuid: str) -> dict:
        rule = parse_rule(policy_data)
        sport_type = int(self.run_cfg.get("sport_type", 1))

        # 学校硬性判据 → 在区间内随机抽一组"里程/配速/步频"，并本地硬校验
        limits = policy.Limits.from_policy(policy_data, sport_type)
        self.log("   学校限制：%s" % limits.describe())
        if limits.raw.get("faceVerify"):
            self.log("   [注意] 本校开启人脸抽检（faceVerify=true），纯协议提交可能被判无效")
        if limits.raw.get("bluetoothVerify"):
            self.log("   [注意] 本校要求蓝牙打卡（bluetoothVerify=true），本工具无法模拟")
        rng = random.Random()
        try:
            rp = policy.choose_run(limits, self.run_cfg, rng)
        except policy.PlanError as exc:
            raise SportError("无法生成合规参数：%s" % exc, -20)
        issues = policy.validate(rp, limits)
        if issues:
            raise SportError("本地合规校验未通过：%s" % "；".join(issues), -21)
        for note in rp.notes:
            self.log("   · " + note)
        target, duration = rp.distance_m, rp.duration_s

        area_id = int(self.run_cfg.get("run_area_id", -1))
        if area_id < 0:
            areas = [a for a in (policy_data or {}).get("runAreaModels") or [] if a.get("valid")]
            selected = [a for a in areas if a.get("select")]
            if len(areas) > 1 and len(selected) != 1:
                raise SportError(
                    "本校有 %d 个跑步区域且未选定（%s）。请在 config.json 里设置 run.run_area_id"
                    % (len(areas), [a.get("id") for a in areas]), -22)
            area_id = pick_area(policy_data)

        checkpoints: List[dict] = []
        if sport_type == 1:
            anchor = anchor_of(fences, area_id)
            if anchor is None:
                raise SportError("未取到校区围栏坐标，无法申请打卡点", -10)
            try:
                data = self.client.points(target, anchor[0], anchor[1], area_id, run_uuid)
                raw_points = as_list(data.get("pointsResModels")) if isinstance(data, dict) else None
                for p in raw_points or []:
                    lat = p.get("lat", p.get("glat"))
                    lon = p.get("lng", p.get("lon", p.get("glon")))
                    if lat in (None, -1) or lon in (None, -1):
                        continue
                    checkpoints.append({
                        "id": p.get("id", 0),
                        "lat": float(lat),
                        "lng": float(lon),
                        "pointName": p.get("pointName") or p.get("name") or "",
                        "isFixed": int(p.get("isFixed", 1) or 1),
                        "isPass": True,
                        "position": p.get("position") or len(checkpoints) + 1,
                    })
                self.log("打卡点：服务端下发 %d 个" % len(checkpoints))
            except SportError as exc:
                self.log("打卡点：接口失败（%s），尝试本地兜底" % exc)
            if not checkpoints:
                if self.run_cfg.get("allow_template_checkpoints"):
                    checkpoints = self._template_checkpoints()
                    if checkpoints:
                        self.log("   [警告] 使用本地模板打卡点（仅当与模板同校区才有效）")
                if not checkpoints:
                    raise SportError(
                        "服务端未下发打卡点，无法生成计分跑轨迹。"
                        "请确认该校区已开放跑步；确需用模板请设置 run.allow_template_checkpoints=true", -23)
        route_pts = [(c["lat"], c["lng"]) for c in checkpoints] if checkpoints else fence_ring(fences)
        if len(route_pts) < 2:
            anchor = anchor_of(fences, area_id) or (0.0, 0.0)
            route_pts = circle_ring(anchor[0], anchor[1])
            self.log("路线：围栏不可用，使用默认环形路线")

        # 坐标系判定：接口下发的打卡点可能是 WGS-84，而地图（gLat/gLng）要 GCJ-02。
        # 拿围栏多边形比一下：原样在内说明已是 GCJ；转一下才在内说明是 WGS。
        fence_poly = []
        for f in fences or []:
            fence_poly = geo.polygon(f.get("points") or [], ("glat", "glon"))
            if len(fence_poly) >= 3:
                break
        mode = geo.detect(checkpoints, fence_poly)
        checkpoints_geo = checkpoints
        if mode in ("bd09", "wgs"):
            checkpoints_geo = geo.convert_points(checkpoints, mode)
            route_pts = [(c["lat"], c["lng"]) for c in checkpoints_geo]
            self.log("   坐标系：%s → 已转换为 GCJ-02（围栏 %d 点；不转地图会偏移约 900m，画不出绿线）"
                     % ("BD-09(百度)" if mode == "bd09" else "WGS-84", len(fence_poly)))
        elif mode == "gcj":
            self.log("   坐标系：GCJ-02（与围栏一致，无需转换）")
        else:
            self.log("   坐标系：无法判定（围栏 %d 点），按原样使用" % len(fence_poly))
        return {
            "rule": rule,
            "limits": limits,
            "run_plan": rp,
            "sport_type": sport_type,
            "distance_m": target,
            "duration_s": duration,
            "area_id": area_id,
            "checkpoints": checkpoints,
            "checkpoints_geo": checkpoints_geo,
            "coord_mode": mode,
            "route": route_pts,
            "policy": rule["policy"],
        }

    def _template_checkpoints(self) -> List[dict]:
        path = store.DATA_BODY
        if not path.exists():
            return []
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
            fp = json.loads(body.get("fivePointJson") or "{}")
            inner = json.loads(fp.get("fivePointJson") or "[]")
            out = []
            for i, p in enumerate(inner):
                lat = p.get("lat", p.get("glat"))
                lon = p.get("lng", p.get("lon", p.get("glon")))
                out.append({"id": p.get("id", 0), "lat": float(lat), "lng": float(lon),
                            "pointName": p.get("pointName") or "模板点%d" % (i + 1),
                            "isFixed": 1, "isPass": True, "position": i + 1})
            if out:
                self.log("打卡点：使用本地模板 %d 个（与模板同校区才有效）" % len(out))
            return out
        except Exception:
            return []

    # -- 当日额度 / 幂等 ----------------------------------------------------
    def already_done(self, sport_type: int) -> Optional[dict]:
        today0 = store.server_midnight_ms()
        try:
            recs = self.client.records(page=1, size=10, sport_type=sport_type)
        except SportError:
            return None
        for rec in recs:
            if int(rec.get("startTime") or 0) < today0:
                continue
            if rec.get("complete") and float(rec.get("validDis") or 0) >= 1000:
                return rec
        return None

    # -- 主流程 ------------------------------------------------------------
    def run(self, dry: bool = False, force: bool = False, poll_sec: int = 600,
            stop_before_submit: bool = False) -> Report:
        started = time.time()
        self.log("① 与服务端对时 ...")
        self.client.sync_time()

        self.log("② 读取账号 ...")
        me = self.profile()
        self.log("   账号：%s（uid=%s, unid=%s, %s）" % (
            me.get("name"), me.get("uid"), me.get("unid"),
            me.get("campusName") or me.get("universityName") or "-"))

        sport_type = int(self.run_cfg.get("sport_type", 1))
        self.log("③ 拉取跑步规则与电子围栏 ...")
        policy_data = self.client.run_policy(run_mode=sport_type) or {}
        rule = parse_rule(policy_data)
        rule_raw = policy_data.get("runRuleModel")
        policy_flag = int(policy_data.get("policy") or 0)
        freeze = int(policy_data.get("freezeRunTime") or 0)
        if rule_raw:
            self.log("   规则：单次≥%dm / %ds，日目标%dm，有效时段 %s" % (
                rule["min_distance"], rule["min_run_time"], rule["day_goal"],
                ",".join("%s-%s" % t for t in rule["valid_times"]) or "不限"))
        else:
            self.log("   规则：服务端未下发（policy=%s，freezeRunTime=%s）" % (policy_flag, freeze))

        # 冻结 / 无规则 → 默认拒绝提交（这是被判违规后最常见的状态）
        if (not rule_raw) or policy_flag == 0 or freeze > 0:
            hint = ""
            if freeze > 0:
                hint = "冻结剩余约 %.1f 天" % (freeze / 86400.0)
            msg = ("服务端未下发跑步规则或账号处于冻结状态（policy=%s, freezeRunTime=%s%s）。"
                   "冻结期内提交大概率被判无效甚至加重处罚。" % (
                       policy_flag, freeze, ("，" + hint) if hint else ""))
            if dry:
                self.log("   [提示] " + msg + "（dry-run 不提交，继续生成报文）")
            elif not (self.run_cfg.get("allow_frozen") or force):
                self.log("   [拒绝] " + msg)
                return Report(False, "blocked", msg)
            else:
                self.log("   [警告] " + msg + "（已按 force/allow_frozen 继续）")
            self.log("   [警告] " + msg + "（已按 force/allow_frozen 继续）")
        if rule["valid_times"] and not in_valid_window(rule["valid_times"]):
            msg = "当前不在有效跑步时段（%s），提交大概率被判无效" % rule["valid_times"]
            if not force:
                return Report(False, "failed", msg)
            self.log("   [警告] " + msg)

        fences = self.client.geo_fences()
        self.log("   围栏：%d 个" % len(fences))

        if not dry and not force:
            max_day = int(self.run_cfg.get("max_runs_per_day") or 1)
            try:
                todays = self.client.today_records()
            except SportError:
                todays = []
            if max_day > 0 and len(todays) >= max_day:
                msg = "今天已有 %d 条记录（上限 %d），不再提交" % (len(todays), max_day)
                self.log("④ " + msg)
                return Report(True, "already", msg)
            hit = self.already_done(sport_type)
            if hit:
                msg = "今天已完成（rrid=%s，有效 %.2fkm），无需重复提交" % (
                    hit.get("rrid"), float(hit.get("validDis") or 0) / 1000.0)
                self.log("④ " + msg)
                return Report(True, "already", msg, rrid=hit.get("rrid"),
                              distance_m=float(hit.get("validDis") or 0))
            lock = store.submit_lock(account=self.account_key)
            if lock:
                # 本地残留的可能是别的账号的提交（切号后常见），必须用本账号记录校验归属
                owned = True
                try:
                    owned = any(r.get("uuid") == lock.get("uuid")
                                for r in self.client.records(page=1, size=10))
                except SportError:
                    owned = True
                if not owned:
                    self.log("④ 本地残留提交（uuid=%s）不属于当前账号，已忽略并清空"
                             % (lock.get("uuid") or "-"))
                    store.clear_submit(account=self.account_key)
                    lock = None
            if lock:
                msg = "上一次提交（rrid=%s）仍在服务端核验，先查结果" % lock.get("rrid")
                self.log("④ " + msg)
                res = self.poll(lock.get("rrid"), poll_sec=60)
                if res.ok:
                    return res
                return Report(False, "pending", msg, rrid=lock.get("rrid"))

        run_uuid = str(uuid.uuid4())
        self.log("④ 申请打卡点 / 规划路线 ...")
        plan = self.resolve_plan(policy_data, fences, run_uuid)

        self.log("⑤ 合成轨迹（目标 %dm，速度 %.2f m/s）..." % (
            plan["distance_m"], plan.get("run_plan").speed_mps if plan.get("run_plan") else 0))
        self.client.sync_time()
        off_lo, off_hi = (self.run_cfg.get("stop_offset_sec") or [0, 0])[:2]
        try:
            offset = random.Random().randint(int(off_lo), int(off_hi)) if int(off_hi) > int(off_lo) else 0
        except Exception:
            offset = 0
        stop_ms = self.client.now_ms() - offset * 1000

        rp = plan.get("run_plan")
        limits = plan.get("limits")
        route_pts = track.build_route(plan["route"])
        tr = None
        actual = None
        issues = []
        for attempt in range(4):
            cand = track.sample_track(route_pts, plan["distance_m"], rp.speed_mps if rp else 2.5,
                                      start_ms=0, cadence=rp.cadence if rp else 170,
                                      run_cfg=self.run_cfg,
                                      seed=random.Random().randint(1, 10 ** 9),
                                      checkpoints=plan["checkpoints"])
            cand_plan = policy.RunPlan(
                distance_m=int(round(cand.distance_m)), duration_s=cand.duration_s,
                steps=cand.steps, pace_sec=cand.pace_sec,
                cadence=(cand.steps * 60.0 / max(1.0, cand.active_s)),
                speed_mps=cand.speed_mps, stride_m=cand.distance_m / max(1, cand.steps))
            issues = policy.validate(cand_plan, limits) if limits else []
            tr, actual = cand, cand_plan
            if not issues:
                break
            self.log("   [重采样 %d] %s" % (attempt + 1, "；".join(issues)))
        if issues:
            raise SportError("轨迹无法满足合规区间：%s" % "；".join(issues), -24)

        start_ms = stop_ms - tr.duration_s * 1000
        # 采样时 start_ms=0，这里把 gainTime / flag 平移到真实开始时刻
        for pt in tr.points:
            pt["gainTime"] = track._fmt_time(track.parse_gain_time(pt["gainTime"]) * 1000 + start_ms)
            pt["flag"] = int(start_ms)
        gps = tr.points
        vertices = tr.path
        steps = tr.steps
        self.log("   本次参数：%s；停顿 %.0fs（结束时刻前移 %ds）" % (
            tr.describe(), tr.pause_s, offset))
        _ints = sorted((track.parse_gain_time(b["gainTime"]) - track.parse_gain_time(a["gainTime"]))
                       for a, b in zip(gps, gps[1:]))
        _sp = [p["speed"] for p in gps]
        _mean = sum(_sp) / len(_sp)
        self.log("   轨迹特征：%d 点 / 采样间隔中位 %.0fs / speed σ=%.2f / radius %.2f–%.2f / 类型 %s" % (
            len(gps), _ints[len(_ints) // 2] if _ints else 0,
            (sum((x - _mean) ** 2 for x in _sp) / len(_sp)) ** 0.5,
            min(p["radius"] for p in gps), max(p["radius"] for p in gps),
            sorted({p["type"] for p in gps})))
        check = track.verify_track(gps, tr.distance_m, tr.duration_s, start_ms,
                                   plan.get("checkpoints_geo") or plan["checkpoints"])
        warnings = []
        if check["missing_checkpoints"]:
            warnings.append("未覆盖打卡点: %s" % check["missing_checkpoints"])
        pace = check["pace_sec_per_km"]
        self.log("   点数=%d 实际里程=%.1fm 配速=%s 步数=%d" % (
            check["points"], check["distance_m"], track.pace_text(pace), steps))
        if pace < 240 or pace > 600:
            warnings.append("配速 %s 偏离常见区间（4'00\"–10'00\"/km）" % track.pace_text(pace))
        for w in warnings:
            self.log("   [警告] " + w)

        body, obs_payload, object_key = track.build_record(
            uid=int(me["uid"]), unid=int(me.get("unid") or 0), run_uuid=run_uuid,
            sport_type=plan["sport_type"], distance_m=int(round(tr.distance_m)),
            duration_s=tr.duration_s, steps=steps, start_ms=start_ms,
            stop_ms=start_ms + tr.duration_s * 1000,
            gps=gps, checkpoints=plan["checkpoints"], route_vertices=vertices,
            sel_distance=max(plan["rule"]["min_distance"], 1000),
            sel_runtime=plan["rule"]["min_run_time"], policy=plan["policy"],
            run_area_id=plan["area_id"], fences_raw=fences,
        )

        if dry:
            out = store.BASE_DIR / "dryrun.json"
            out.write_text(json.dumps({"objectKey": object_key, "body": body,
                                       "obs": obs_payload, "check": check},
                                      ensure_ascii=False, indent=1), encoding="utf-8")
            self.log("dry-run：已写入 %s（未联网提交）" % out)
            return Report(True, "dry", "dry-run 完成", uuid=run_uuid,
                          distance_m=check["distance_m"], duration_s=plan["duration_s"],
                          steps=steps, pace=track.pace_text(pace),
                          checkpoints=len(plan["checkpoints"]), warnings=warnings)

        self.log("⑥ 申请对象存储直传地址 ...")
        obs = self.client.obs_url(object_key)
        signed_url = obs.get("signedUrl") or obs.get("url")
        if not signed_url:
            raise SportError("未取到 OBS 预签名地址：%s" % json.dumps(obs)[:160], -11)
        headers = obs.get("actualSignedRequestHeaders") or {"Content-Type": "text/plain"}
        payload = json.dumps(obs_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        status = self.client.put_object(signed_url, payload, headers)
        self.log("   轨迹已上传（PUT %s，%d bytes）" % (status, len(payload)))

        if stop_before_submit:
            self.log("⑦ [stop-before-submit] 轨迹已上传，未提交结算（排障模式）")
            return Report(True, "dry", "已上传轨迹但未提交（stop-before-submit）",
                          uuid=run_uuid, distance_m=check["distance_m"],
                          duration_s=tr.duration_s, steps=steps,
                          pace=track.pace_text(pace), checkpoints=len(plan["checkpoints"]),
                          warnings=warnings)

        # 提交前基线：用于证明"提交后学期里程确实变了"
        baseline: dict = {}
        try:
            sem = self.client.semester_summary()
            baseline = {"sid": sem.get("_sid"), "semesterDis": sem.get("semesterDis"),
                        "semesterValidDis": sem.get("semesterValidDis")}
            self.log("   提交前学期里程：%s m（有效 %s m，sid=%s）" % (
                baseline.get("semesterDis"), baseline.get("semesterValidDis"), baseline.get("sid")))
        except SportError as exc:
            self.log("   [警告] 读取学期基线失败：%s" % exc)

        self.log("   数据体检：%s；步幅 %.2fm；人类可信区间 速度≤%.1f m/s、步频≤%d" % (
            rp.describe() if rp else "-", rp.stride_m if rp else 0,
            float((self.run_cfg.get("human_speed_mps") or [0, 4.2])[1]),
            int((self.run_cfg.get("human_cadence_spm") or [0, 190])[1])))
        self.log("⑦ 提交结算 ...")
        res = self.client.save_record(body)
        rrid = res.get("rrid") or res.get("id")
        if not rrid:
            # 结算接口有时不回 rrid，按 uuid 回查刚提交的记录
            self.log("   结算响应未带 rrid，按 uuid 回查记录 ...")
            for _ in range(12):
                try:
                    for rec in self.client.records(page=1, size=5):
                        if rec.get("uuid") == run_uuid:
                            rrid = rec.get("rrid")
                            break
                except SportError:
                    pass
                if rrid:
                    break
                time.sleep(5)
        store.mark_submitted(run_uuid, rrid, int(round(tr.distance_m)), tr.duration_s,
                             account=self.account_key,
                             object_key=object_key, upload={"status": status, "bytes": len(payload)},
                             baseline=baseline, steps=steps)
        if rrid:
            self.log("   已受理 rrid=%s，进入服务端核验" % rrid)
        else:
            self.log("   已提交，但暂时查不到记录号；稍后用 status 复查")

        result = self.poll(rrid, poll_sec=poll_sec)
        result.uuid = run_uuid
        result.distance_m = result.distance_m or check["distance_m"]
        result.duration_s = tr.duration_s
        result.steps = steps
        result.pace = track.pace_text(pace)
        result.checkpoints = len(plan["checkpoints"])
        result.warnings = warnings
        result.detail["elapsed_sec"] = round(time.time() - started, 1)
        # 提交后再取一次服务端事实，放进报告里给界面展示
        verify = self.verify(run_uuid)
        result.detail["verify"] = verify
        store.update_submit(
            account=self.account_key,
            verdict={
                "ok": result.ok,
                "status": result.status,
                "complete": (verify.get("detail") or {}).get("complete"),
                "validDis": (verify.get("detail") or {}).get("validDis"),
                "statusInfo": (verify.get("detail") or {}).get("statusInfo"),
                "rrid": (verify.get("record") or {}).get("rrid") or rrid,
            },
            semester_after=verify.get("semester") or {},
        )
        store.mark_result(result.ok, {"rrid": rrid, "status": result.status},
                          account=self.account_key)
        self._log_verification(verify, baseline)
        return result

    # -- 核对 --------------------------------------------------------------
    def verify(self, run_uuid: str = "") -> dict:
        try:
            return self.client.verify_submission(run_uuid) or {}
        except Exception as exc:      # 核对失败不能影响主流程
            return {"error": str(exc)}

    def _log_verification(self, verify: dict, baseline: dict) -> None:
        rec = verify.get("record") or {}
        detail = verify.get("detail") or {}
        if rec:
            self.log("   [服务端记录] rrid=%s complete=%s 有效=%sm 状态=%s" % (
                rec.get("rrid"), detail.get("complete"), detail.get("validDis"),
                detail.get("statusInfo") or "-"))
            if detail.get("jsonFromObs") is not None:
                self.log("   [轨迹落地] 服务端 jsonFromObs=%s（1 表示已从对象存储读到轨迹）" %
                         detail.get("jsonFromObs"))
        sem = verify.get("semester") or {}
        if sem and baseline.get("semesterDis") is not None:
            try:
                delta = float(sem.get("semesterDis") or 0) - float(baseline.get("semesterDis") or 0)
                self.log("   [学期变化] %s m → %s m（%+.0f m），有效 %s m" % (
                    baseline.get("semesterDis"), sem.get("semesterDis"), delta,
                    sem.get("semesterValidDis")))
            except Exception:
                pass

    # -- 核验 --------------------------------------------------------------
    def poll(self, rrid: Any, poll_sec: int = 600, interval: int = 10) -> Report:
        """轮询核验。

        真实语义（以 5 条历史记录校准）：
            成功：complete == true, isCheat == 0, validDis > 0   —— 注意 status 仍是 0，
                  statusInfo 类似"太棒了！恭喜你，本次跑步挑战成功…"
            失败：complete == false 且 unCompleteReason 非 0
        旧实现要求 status == 1，会永远等不到"成功"。
        """
        if rrid in (None, "", 0):
            return Report(False, "pending", "服务端未返回 rrid，稍后可在 App 内查看")
        self.log("⑧ 轮询核验结果（最长 %ds）..." % poll_sec)
        deadline = time.time() + poll_sec
        last: dict = {}
        while time.time() < deadline:
            try:
                last = self.client.one_record(rrid) or {}
                if last:
                    complete = last.get("complete")
                    reason = last.get("unCompleteReason")
                    valid = float(last.get("validDis") or 0)
                    cheat = int(last.get("isCheat") or 0)
                    info = str(last.get("statusInfo") or "")
                    if complete and cheat == 0 and valid > 0:
                        msg = "核验通过：有效里程 %.2fkm，用时 %ds%s" % (
                            valid / 1000.0, int(last.get("totalTime") or 0),
                            ("（" + info + "）") if info else "")
                        self.log("   " + msg)
                        return Report(True, "done", msg, rrid=rrid, distance_m=valid,
                                      duration_s=int(last.get("totalTime") or 0), detail=last)
                    if complete is False and reason not in (None, 0, "0"):
                        msg = "判定未通过：unCompleteReason=%s（%s）" % (reason, info)
                        self.log("   " + msg)
                        return Report(False, "failed", msg, rrid=rrid, detail=last)
                    self.log("   已保存，等待判定 …（status=%s complete=%s）" % (
                        last.get("status"), complete))
            except SportError as exc:
                self.log("   查询失败：%s" % exc)
            time.sleep(interval)
        msg = "提交已受理，但 %ds 内未出结果；稍后运行  一键跑步.bat status  复查" % poll_sec
        return Report(False, "pending", msg, rrid=rrid, detail=last)
