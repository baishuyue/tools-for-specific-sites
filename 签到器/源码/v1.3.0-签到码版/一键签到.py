# -*- coding: utf-8 -*-
"""
优学院(uLearning / DGUT) 一键签到 —— 纯 PC 接口版，不依赖安卓模拟器 / 定位软件。

核心原理（已通过真实接口实证）：
  1. 定位签到(type=0) 时，学生端接口
        GET /apps/newAttendance/getAttendanceForStu/{点名ID}/{本人userID}
     返回的 location 字段就是【教师发起签到时的中心点(百度 BD09 坐标)】、
     address 为中心点的逆地理地址、classez[0].classID 为签到所需班级ID。
     —— 该 location 对全班任意学生都相同，即“教师设定的点”。
  2. 直接把“中心点 + 3~10 米随机偏移”作为自己的定位提交给 signByStu，
     服务端计算的距离恒在 0~10 米，远小于签到半径(实测约 100~150 米)，
     因此无论老师在哪一栋楼发起、学生本人在何处，都能一键签到成功。
  3. 数字码(type=1) / 二维码(type=2) 签到：需要老师给出的码，用 --code 传入；
     定位签到无需任何输入，全自动。

用法：
    python 一键签到.py                 # 扫描所有课程，自动签掉所有“进行中且未签”的定位签到
    python 一键签到.py --code 123456   # 同时处理数字码/二维码签到（码为 123456）
    python 一键签到.py --watch         # 持续监听，老师一发起签到就在数秒内自动签
    python 一键签到.py --dry-run       # 只看会怎么签，不真正提交
    python 一键签到.py --user 登录名    # 指定配置文件里的账号（默认 activeUser 或唯一账号）

账号配置：同目录 ulearning-cmd.json，格式与现有文件一致：
    {"users": {"登录名": {"password": "密码"}}, "activeUser": "登录名"}
"""
from __future__ import annotations

# build: 626169736875797565-2026
__build_ref__ = "YmFpc2h1eXll"

import argparse
import json
import math
import os
import random
import sys
import time
from pathlib import Path
from urllib.parse import unquote

try:
    import requests
except ImportError:
    print("缺少 requests，请先运行：python -m pip install requests")
    raise

LMS = "https://lms.dgut.edu.cn"
APP = "https://application.dgut.edu.cn"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
EARTH_R = 6378137.0
# 打包成 exe 后配置文件放在 exe 同目录；源码运行时放在脚本同目录。
BASE_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
DEFAULT_CONFIG = BASE_DIR / "ulearning-cmd.json"

# 签到类型（getAttendanceForStu 返回的 type）
TYPE_LOCATION = 0   # 定位签到
TYPE_CODE = 1       # 数字码签到
TYPE_QR = 2         # 二维码签到


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def jitter_bd09(lng: float, lat: float, min_m: float = 3.0, max_m: float = 10.0) -> tuple[float, float]:
    """在 BD09 经纬度点周围随机偏移 min_m~max_m 米，返回 (经度, 纬度)，保持同一坐标系。"""
    dist = random.uniform(min_m, max_m)
    ang = random.uniform(0, 2 * math.pi)
    dlat = (dist * math.cos(ang)) / EARTH_R * 180.0 / math.pi
    dlng = (dist * math.sin(ang)) / (EARTH_R * math.cos(math.radians(lat))) * 180.0 / math.pi
    return lng + dlng, lat + dlat


class Ulearning:
    def __init__(self, login_name: str, password: str, timeout: float = 20.0):
        self.login_name = login_name
        self.password = password
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept": "application/json", "version": "1"})
        self.user_id = 0
        self.name = ""
        self.token = ""

    # ---------- 登录 ----------
    def login(self) -> None:
        resp = self.s.post(
            APP + "/appapi/user/login/app",
            data={"loginName": self.login_name, "password": self.password, "alias": "application"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            allow_redirects=False, timeout=self.timeout,
        )
        token = resp.cookies.get("AUTHORIZATION")
        userinfo = resp.cookies.get("USERINFO")
        if not token:
            raise RuntimeError(f"登录失败：未取到 token（HTTP {resp.status_code}），检查账号密码/网络")
        self.token = token
        self.s.headers["AUTHORIZATION"] = token
        if userinfo:
            try:
                info = json.loads(unquote(userinfo))
                self.user_id = int(info.get("userId") or info.get("userID") or 0)
                self.name = info.get("name", "")
            except Exception:
                pass
        if not self.user_id:
            raise RuntimeError("登录成功但未解析到 userId")
        log(f"登录成功：{self.name or ''}（{self.login_name}，userID={self.user_id}）")

    # ---------- 课程 / 活动 ----------
    def list_courses(self) -> list[dict]:
        d = self.s.get(LMS + "/courseapi/courses/students",
                       params={"keyword": "", "publishStatus": 1, "type": 1,
                               "pn": 1, "ps": 300, "lang": "zh"},
                       timeout=self.timeout).json()
        return d.get("courseList") or d.get("list") or []

    def list_activities(self, course_id: str) -> list[dict]:
        out, pn = [], 1
        while True:
            d = self.s.get(LMS + f"/courseapi/classActivity/stu/{course_id}/-1",
                           params={"pn": pn, "ps": 50, "lang": "zh"},
                           timeout=self.timeout).json()
            items = d.get("list") or []
            if not items:
                break
            out.extend(items)
            if len(items) < 50:
                break
            pn += 1
        return out

    def attendance_info(self, activity_id) -> dict:
        """学生端签到配置：type=0 时 location 为教师中心点(BD09)。"""
        r = self.s.get(LMS + f"/apps/newAttendance/getAttendanceForStu/{activity_id}/{self.user_id}",
                       timeout=self.timeout)
        try:
            return r.json()
        except Exception:
            return {}

    # ---------- 提交签到 ----------
    # 服务端状态码（实测）：200=成功，202=点名已结束，209=不在签到时间/不可签；成功时 newStatus=1。
    def sign(self, activity_id, class_id, location="", address="", code="", retries: int = 2) -> dict:
        payload = {
            "attendanceID": int(activity_id),
            "classID": int(class_id) if str(class_id).isdigit() else class_id,
            "userID": self.user_id,
            "location": location or "",
            "address": address or "",
            "enterWay": 1,
            "attendanceCode": code or "",
        }
        last = {"msg": "未知错误", "status": -1}
        for attempt in range(retries + 1):
            try:
                resp = self.s.post(APP + "/classroomapi/newAttendance/signByStu",
                                   json=payload, timeout=self.timeout)
                text = resp.text.strip()
                if resp.status_code == 200 and text.startswith("{"):
                    return resp.json()
                last = {"msg": f"HTTP {resp.status_code}：{text[:120]}", "status": resp.status_code}
            except Exception as e:
                last = {"msg": f"请求异常：{e}", "status": -1}
            time.sleep(1.0 * (attempt + 1))   # 限流/空响应时退避重试
        return last

    @staticmethod
    def is_success_response(resp: dict) -> tuple[bool, str]:
        status = str(resp.get("status"))
        new_status = str(resp.get("newStatus"))
        msg = resp.get("msg") or resp.get("message") or ""
        if status == "200" and new_status == "1":
            return True, msg or "签到成功"
        if "成功" in msg:
            return True, msg
        if "已签" in msg:
            return True, "已签到"
        reason = msg or {"202": "点名已结束", "209": "不在签到时间/不可签"}.get(status, f"服务端 status={status}")
        return False, reason

    def is_signed(self, activity_id) -> bool:
        """提交后复核：本人是否已签到。"""
        try:
            info = self.attendance_info(activity_id)
            return str(info.get("status")) == "1" or str(info.get("state")) == "1"
        except Exception:
            return False


def ensure_config(config_path: Path) -> None:
    """配置文件缺失或为空时，首次运行交互式引导输入账号并保存。"""
    cfg = {}
    if config_path.exists():
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    users = cfg.get("users") or {}
    valid = {k: v for k, v in users.items() if v.get("password")}
    if valid:
        return
    print("=" * 60)
    print("首次使用，请输入优学院账号（只需一次，会保存在同目录配置文件）")
    login_name = input("登录名/学号（如 dgut2024xxxxxxxxx）：").strip()
    password = input("密码：").strip()
    if not login_name or not password:
        raise RuntimeError("账号或密码为空，已取消")
    cfg.setdefault("users", {})[login_name] = {"password": password}
    cfg["activeUser"] = login_name
    config_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已保存配置到：{config_path}\n" + "=" * 60)


def load_accounts(config_path: Path, user: str | None):
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    users = cfg.get("users", {})
    if not users:
        raise RuntimeError("配置文件里没有 users")
    if user:
        keys = [user]
    else:
        active = cfg.get("activeUser")
        if active and active in users and active != "你的账号":
            keys = [active]
        else:
            keys = list(users.keys())
    result = []
    for k in keys:
        u = users[k]
        # 键名兼容 loginName / userName
        login = u.get("loginName") or u.get("userName") or k
        result.append((login, u.get("password", "")))
    return result


def process_once(api: Ulearning, code: str, dry_run: bool) -> tuple[int, int]:
    """扫描并处理所有进行中未签的点名。返回 (成功数, 跳过数)。"""
    ok = skip = 0
    courses = api.list_courses()
    targets = []
    for c in courses:
        cid = str(c.get("courseID") or c.get("id") or c.get("courseId"))
        cname = c.get("courseName") or c.get("name") or cid
        try:
            acts = api.list_activities(cid)
        except Exception as e:
            log(f"  课程《{cname}》活动拉取失败：{e}")
            continue
        for a in acts:
            if str(a.get("relationType")) != "1":
                continue
            if str(a.get("timeStatus")) != "2":      # 仅进行中
                continue
            if str(a.get("personStatus")) == "1":    # 已签
                continue
            targets.append((cid, cname, a))
        time.sleep(0.03)

    if not targets:
        log("当前没有进行中且未签到的点名。")
        return 0, 0

    for cid, cname, a in targets:
        aid = a.get("relationId")
        title = a.get("title", "")
        log(f"发现待签：《{cname}》{title}（点名ID {aid}）")
        info = api.attendance_info(aid)
        atype = info.get("type")
        classes = info.get("classez") or []
        class_id = classes[0].get("classID") if classes else ""
        center = (info.get("location") or "").strip()
        addr = (info.get("address") or "").strip()

        location = address = use_code = ""
        if atype == TYPE_LOCATION:
            if not center:
                log("  ⚠ 是定位签到但接口未返回中心点，跳过（可稍后重试）")
                skip += 1
                continue
            lng0, lat0 = map(float, center.split(","))
            lng, lat = jitter_bd09(lng0, lat0)
            location = f"{lng:.6f},{lat:.6f}"
            address = addr
            log(f"  定位签到：教师中心点 {center}（{addr}），提交点 {location}（偏移<10m）")
        elif atype in (TYPE_CODE, TYPE_QR):
            if not code:
                kind = "数字码" if atype == TYPE_CODE else "二维码"
                log(f"  ⚠ {kind}签到，需要老师给出的码，本次跳过（用 --code 码 处理）")
                skip += 1
                continue
            use_code = code
            log(f"  {'数字码' if atype == TYPE_CODE else '二维码'}签到：提交码 {code}")
        else:
            log(f"  ⚠ 未知签到类型 type={atype}，跳过")
            skip += 1
            continue

        if dry_run:
            log("  [dry-run] 未提交。")
            continue

        try:
            resp = api.sign(aid, class_id, location=location, address=address, code=use_code)
        except Exception as e:
            log(f"  ✗ 提交异常：{e}")
            skip += 1
            continue
        success, reason = api.is_success_response(resp)
        if not success and api.is_signed(aid):
            success, reason = True, "已签到（复核确认）"
        if success:
            log(f"  ✓ 签到成功：{reason}")
            ok += 1
        else:
            log(f"  ✗ 签到失败：{reason}")
            skip += 1
        time.sleep(0.8)
    return ok, skip


def main() -> int:
    p = argparse.ArgumentParser(description="优学院一键签到（定位签到自适应教师中心点）")
    p.add_argument("--config", default=str(DEFAULT_CONFIG))
    p.add_argument("--user", default=None, help="配置文件中的登录名；默认 activeUser 或唯一账号")
    p.add_argument("--code", default="", help="数字码/二维码签到码（定位签到不需要）")
    p.add_argument("--dry-run", action="store_true", help="只演练不提交")
    p.add_argument("--watch", action="store_true", help="持续监听，每 20 秒检查一次")
    p.add_argument("--interval", type=int, default=20, help="监听模式检查间隔秒数，默认 20")
    args = p.parse_args()

    if os.name == "nt":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    config_path = Path(args.config)
    if not args.dry_run:
        ensure_config(config_path)
    accounts = load_accounts(config_path, args.user)
    apis = []
    for login_name, password in accounts:
        api = Ulearning(login_name, password)
        api.login()
        apis.append(api)

    if not args.watch:
        total_ok = total_skip = 0
        for api in apis:
            ok, sk = process_once(api, args.code, args.dry_run)
            total_ok += ok
            total_skip += sk
        log(f"完成：成功 {total_ok} 个，跳过/失败 {total_skip} 个。")
        return 0 if total_skip == 0 or total_ok else 1

    log(f"进入监听模式，每 {args.interval} 秒检查一次（Ctrl+C 退出）……")
    try:
        while True:
            for api in apis:
                try:
                    process_once(api, args.code, args.dry_run)
                except Exception as e:
                    log(f"本轮出错（下轮继续）：{e}")
                    try:
                        api.login()
                    except Exception:
                        pass
            time.sleep(args.interval)
    except KeyboardInterrupt:
        log("已退出监听。")
        return 0


if __name__ == "__main__":
    sys.exit(main())
