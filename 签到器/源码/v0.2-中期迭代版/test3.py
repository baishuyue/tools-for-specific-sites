"""Single-file Ulearning course/activity tool.

Run without arguments to open the Tkinter window. The CLI is also available:

    python ulearning_standalone.py list-courses
    python ulearning_standalone.py list-selection --active-only
    python ulearning_standalone.py signin --course-id COURSE_ID --activity-id ID --dry-run

The activity list follows the same student endpoint used by the official web
client. Selection state is read from the server response. Attendance is a
single, user-initiated POST, either for a selected activity or the only
unsigned attendance activity. Ended activities are also offered for a
server-supported make-up request; this tool never writes selection state or
promotes a student session into a teacher session.
"""

from __future__ import annotations

# build: 626169736875797565-2026
__build_ref__ = "YmFpc2h1eXll"

import argparse
import json
import logging
import math
import os
import re
import sys
import threading
import time
import webbrowser
from collections.abc import Callable, Iterable, Mapping, MutableMapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin

import requests


LOGGER = logging.getLogger("ulearning_standalone")
LMS_BASE_URL = "https://lms.dgut.edu.cn"
APPLICATION_BASE_URL = "https://application.dgut.edu.cn"
OFFICIAL_PAGE = "https://lms.dgut.edu.cn/courseweb/ulearning/index.html#/index/courseList"
FIXED_LOCATION = "113.879022,22.901269"
COORD_SYSTEMS = ("gcj02", "wgs84", "bd09")
RELATION_TYPE = {
    1: "点名",
    2: "投票",
    3: "抢答",
    4: "选人",
    5: "打分",
    14: "拍照上传",
    18: "答题",
    25: "测验",
}


def _configure_console_encoding() -> None:
    if os.name != "nt":
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


class ConfigError(ValueError):
    pass


class ApiError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class AuthenticationError(ApiError):
    pass


class SchemaError(ApiError):
    pass


@dataclass(slots=True)
class AppConfig:
    version: int = 2
    lms_base_url: str = LMS_BASE_URL
    application_base_url: str = APPLICATION_BASE_URL
    auth_token: str = ""
    user_id: int = 0
    role_id: int = 0
    timeout: float = 10.0
    retries: int = 2
    backoff: float = 0.5
    activity_page_size: int = 100
    verify_delay: float = 1.5
    history_file: str = "ulearning_history.json"
    login_name: str = ""
    login_password: str = ""
    default_location: str = FIXED_LOCATION
    default_address: str = ""
    default_coord_system: str = "gcj02"


@dataclass(frozen=True, slots=True)
class Course:
    course_id: str
    name: str
    class_id: str
    class_name: str

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "Course":
        return cls(
            course_id=str(payload.get("id", "")),
            name=str(payload.get("name", payload.get("courseName", ""))),
            class_id=str(payload.get("classId", "")),
            class_name=str(payload.get("className", "")),
        )


@dataclass(frozen=True, slots=True)
class SelectionActivity:
    course_id: str
    course_name: str
    classroom_id: str
    classroom_title: str
    activity_id: str
    title: str
    status_code: Any
    status: str
    time_display: str | None
    start_time: str | None
    end_time: str | None
    score: Any
    person_status: Any
    time_status: Any
    selection_state: str
    selected: bool | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _as_int(value: Any, default: int | None = None) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _first(payload: Mapping[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return None


def _join_url(base_url: str, path: str) -> str:
    return urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))


def _format_time(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000
        if number > 1_000_000_000:
            return datetime.fromtimestamp(number, tz=timezone.utc).astimezone().strftime(
                "%Y-%m-%d %H:%M:%S"
            )
    return str(value)


def _activity_times(payload: Mapping[str, Any]) -> tuple[str | None, str | None]:
    return (
        _format_time(_first(payload, ("startTime", "beginTime", "startDate", "startAt"))),
        _format_time(_first(payload, ("endTime", "finishTime", "endDate", "endAt"))),
    )


def activity_status(payload: Mapping[str, Any]) -> str:
    # The official endpoint exposes temporal state as timeDisplay/timeStatus.
    # `status` is activity-specific: for attendance it is the student's result.
    text = _first(payload, ("timeDisplay", "activityStatusName", "statusText"))
    if text:
        text = str(text).strip()
        if "未开始" in text:
            return "未开始"
        if "结束" in text:
            return "已结束"
        if "进行" in text or "开放" in text:
            return "进行中"

    time_status = _as_int(payload.get("timeStatus"))
    if time_status is not None:
        return {1: "未开始", 2: "进行中", 3: "已结束"}.get(time_status, "未知")

    start_value = _first(payload, ("startTime", "beginTime", "startDate", "startAt"))
    end_value = _first(payload, ("endTime", "finishTime", "endDate", "endAt"))
    now = time.time()
    start = _timestamp_seconds(start_value)
    end = _timestamp_seconds(end_value)
    if start is not None and now < start:
        return "未开始"
    if end is not None and now >= end:
        return "已结束"
    if start is not None:
        return "进行中"
    return "未知"


def _timestamp_seconds(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000
        return number if number > 1_000_000_000 else None
    return None


_LOCATION_NUMBER_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")
_GCJ_PI = math.pi
_GCJ_A = 6378245.0
_GCJ_EE = 0.00669342162296594323


def _normalize_coord_system(value: Any, *, fallback: str = "gcj02") -> str:
    normalized = str(value or "").strip().lower().replace("-", "")
    aliases = {
        "gcj": "gcj02",
        "gcj02": "gcj02",
        "mars": "gcj02",
        "wgs": "wgs84",
        "wgs84": "wgs84",
        "gps": "wgs84",
        "bd": "bd09",
        "bd09": "bd09",
        "baidu": "bd09",
    }
    return aliases.get(normalized, fallback)


def _valid_lon_lat(lon: float, lat: float) -> bool:
    return -180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0


def _looks_like_china_lon_lat(lon: float, lat: float) -> bool:
    return 70.0 <= lon <= 140.0 and 0.0 <= lat <= 60.0


def _coerce_lon_lat(first: float, second: float) -> tuple[float, float, bool]:
    if _looks_like_china_lon_lat(first, second):
        return first, second, False
    if _looks_like_china_lon_lat(second, first):
        return second, first, True
    if abs(first) <= 90.0 < abs(second):
        return second, first, True
    return first, second, False


def _out_of_china(lon: float, lat: float) -> bool:
    return not (72.004 <= lon <= 137.8347 and 0.8293 <= lat <= 55.8271)


def _transform_lat(lon_offset: float, lat_offset: float) -> float:
    value = -100.0 + 2.0 * lon_offset + 3.0 * lat_offset
    value += 0.2 * lat_offset * lat_offset + 0.1 * lon_offset * lat_offset
    value += 0.2 * math.sqrt(abs(lon_offset))
    value += (20.0 * math.sin(6.0 * lon_offset * _GCJ_PI) + 20.0 * math.sin(2.0 * lon_offset * _GCJ_PI)) * 2.0 / 3.0
    value += (20.0 * math.sin(lat_offset * _GCJ_PI) + 40.0 * math.sin(lat_offset / 3.0 * _GCJ_PI)) * 2.0 / 3.0
    value += (160.0 * math.sin(lat_offset / 12.0 * _GCJ_PI) + 320.0 * math.sin(lat_offset * _GCJ_PI / 30.0)) * 2.0 / 3.0
    return value


def _transform_lon(lon_offset: float, lat_offset: float) -> float:
    value = 300.0 + lon_offset + 2.0 * lat_offset
    value += 0.1 * lon_offset * lon_offset + 0.1 * lon_offset * lat_offset
    value += 0.1 * math.sqrt(abs(lon_offset))
    value += (20.0 * math.sin(6.0 * lon_offset * _GCJ_PI) + 20.0 * math.sin(2.0 * lon_offset * _GCJ_PI)) * 2.0 / 3.0
    value += (20.0 * math.sin(lon_offset * _GCJ_PI) + 40.0 * math.sin(lon_offset / 3.0 * _GCJ_PI)) * 2.0 / 3.0
    value += (150.0 * math.sin(lon_offset / 12.0 * _GCJ_PI) + 300.0 * math.sin(lon_offset / 30.0 * _GCJ_PI)) * 2.0 / 3.0
    return value


def _wgs84_to_gcj02(lon: float, lat: float) -> tuple[float, float]:
    if _out_of_china(lon, lat):
        return lon, lat
    dlat = _transform_lat(lon - 105.0, lat - 35.0)
    dlon = _transform_lon(lon - 105.0, lat - 35.0)
    radlat = lat / 180.0 * _GCJ_PI
    magic = math.sin(radlat)
    magic = 1.0 - _GCJ_EE * magic * magic
    sqrt_magic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_GCJ_A * (1.0 - _GCJ_EE)) / (magic * sqrt_magic) * _GCJ_PI)
    dlon = (dlon * 180.0) / (_GCJ_A / sqrt_magic * math.cos(radlat) * _GCJ_PI)
    return lon + dlon, lat + dlat


def _bd09_to_gcj02(lon: float, lat: float) -> tuple[float, float]:
    x = lon - 0.0065
    y = lat - 0.006
    z = math.sqrt(x * x + y * y) - 0.00002 * math.sin(y * _GCJ_PI)
    theta = math.atan2(y, x) - 0.000003 * math.cos(x * _GCJ_PI)
    return z * math.cos(theta), z * math.sin(theta)


def _format_location(lon: float, lat: float) -> str:
    return f"{lon:.6f},{lat:.6f}"


def _prepare_location(location: Any, coord_system: Any) -> tuple[str, dict[str, Any] | None]:
    raw = str(location or "").strip()
    if not raw:
        return "", None
    values = _LOCATION_NUMBER_RE.findall(raw)
    if len(values) < 2:
        raise ConfigError("签到位置必须包含经度和纬度，例如 113.879022,22.901269")
    first, second = float(values[0]), float(values[1])
    lon, lat, swapped = _coerce_lon_lat(first, second)
    if not _valid_lon_lat(lon, lat):
        raise ConfigError(f"签到位置超出经纬度范围: {raw}")
    normalized_system = _normalize_coord_system(coord_system)
    sent_lon, sent_lat = lon, lat
    converted_from = ""
    if normalized_system == "wgs84":
        sent_lon, sent_lat = _wgs84_to_gcj02(lon, lat)
        converted_from = "wgs84"
    elif normalized_system == "bd09":
        sent_lon, sent_lat = _bd09_to_gcj02(lon, lat)
        converted_from = "bd09"

    sent = _format_location(sent_lon, sent_lat)
    diagnostic: dict[str, Any] = {
        "input_location": raw,
        "sent_location": sent,
        "coord_system": normalized_system,
        "input_order": "lat,lon" if swapped else "lon,lat",
    }
    if swapped:
        diagnostic["normalized_order"] = "lon,lat"
    if converted_from:
        diagnostic["converted"] = f"{converted_from}->gcj02"
    return sent, diagnostic


def _location_failure_diagnostic(
    message: str, location_diagnostic: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    if "距离老师太远" not in message and "定位" not in message:
        return None
    diagnostic = dict(location_diagnostic or {})
    diagnostic.update(
        {
            "server_rejected_distance": True,
            "hint": (
                "服务端已按定位点名校验距离。请使用教室内手机定位得到的经纬度；"
                "若坐标来自系统 GPS，选择 wgs84；若来自百度地图，选择 bd09。"
            ),
        }
    )
    return diagnostic


def _location_from_mapping(payload: Mapping[str, Any]) -> str:
    direct = _first(payload, ("default_location", "location", "lonlat", "lnglat", "coordinate", "coordinates"))
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    if isinstance(direct, Sequence) and not isinstance(direct, (str, bytes)) and len(direct) >= 2:
        return f"{direct[0]},{direct[1]}"
    nested = _first(payload, ("Location", "locationConfig", "geo"))
    if isinstance(nested, Mapping):
        lat = _first(nested, ("lat", "latitude"))
        lon = _first(nested, ("lon", "lng", "longitude"))
        if lat not in (None, "") and lon not in (None, ""):
            return f"{lon},{lat}"
    lat = _first(payload, ("lat", "latitude"))
    lon = _first(payload, ("lon", "lng", "longitude"))
    if lat not in (None, "") and lon not in (None, ""):
        return f"{lon},{lat}"
    return ""


def _location_field_names(payload: Mapping[str, Any]) -> list[str]:
    markers = (
        "location",
        "longitude",
        "latitude",
        "lon",
        "lng",
        "address",
        "radius",
        "distance",
        "gps",
        "coord",
    )
    return sorted(
        str(key)
        for key in payload
        if any(marker in str(key).lower() for marker in markers)
    )


def activity_is_ended(payload: Mapping[str, Any]) -> bool:
    """Detect a closed activity without relying on localized display text."""
    if _as_int(payload.get("timeStatus")) == 3:
        return True
    text = str(_first(payload, ("timeDisplay", "activityStatusName", "statusText")) or "").lower()
    if any(mark in text for mark in ("结束", "已结束", "����")):
        return True
    end_value = _first(payload, ("endTime", "finishTime", "endDate", "endAt"))
    end = _timestamp_seconds(end_value)
    return end is not None and time.time() >= end


def activity_is_active(payload: Mapping[str, Any]) -> bool:
    return activity_status(payload) == "进行中"


def signable_attendance_activities(
    activities: Iterable[Mapping[str, Any]],
    *,
    allow_ended: bool = True,
    signed_checker: Callable[[Mapping[str, Any]], bool] | None = None,
) -> list[dict[str, Any]]:
    """Return unsigned attendance activities that can receive a sign request.

    Ended activities are included by default so a late request can still be
    attempted when the server accepts it.  ``signed_checker`` lets the GUI
    suppress activities that were accepted locally but whose list response is
    still catching up.
    """
    return [
        dict(activity)
        for activity in activities
        if (signed_checker is None or not signed_checker(activity))
        and AttendanceService.can_sign(activity, allow_ended=allow_ended)[0]
    ]


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y", "是", "已选中", "已选择"}:
            return True
        if normalized in {"false", "0", "no", "n", "否", "未选中", "未选择"}:
            return False
    return None


def selection_state(payload: Mapping[str, Any]) -> tuple[str, bool | None]:
    true_keys = (
        "selected",
        "isSelected",
        "isSelect",
        "chosen",
        "isChosen",
        "joined",
        "isJoined",
        "selectedByTeacher",
        "isSelectedByTeacher",
    )
    false_keys = ("notSelected", "isNotSelected")
    state_keys = (
        "selectionStatus",
        "selectStatus",
        "selectionState",
        "selectState",
        "studentStatus",
        "joinStatus",
        "participationStatus",
        "personStatusName",
        "statusName",
    )

    for key in true_keys:
        if key in payload:
            value = _as_bool(payload[key])
            if value is True:
                return "已选中", True
            if value is False:
                return "未选中", False

    for key in false_keys:
        if key in payload and _as_bool(payload[key]) is True:
            return "未选中", False

    for key in state_keys:
        value = payload.get(key)
        if value is None:
            continue
        text = str(value).strip().lower()
        if any(mark in text for mark in ("已选中", "已选择", "已加入", "selected", "chosen")):
            if not any(mark in text for mark in ("未选", "not selected", "unselected")):
                return "已选中", True
        if any(mark in text for mark in ("未选中", "未选择", "未加入", "unselected")):
            return "未选中", False
        if any(mark in text for mark in ("待确认", "待选择", "等待", "pending")):
            return "待确认", None

    if _as_int(payload.get("relationType")) in (3, 4):
        # Empirical finding from the user's real historical selection record:
        # for relationType=4, personStatus=2 appeared together with score=2
        # after a student was actually selected. Treat personStatus=2 as a
        # strong positive signal, while retaining non-null score as a fallback
        # because an active selected record was also observed with personStatus=1.
        if _as_int(payload.get("personStatus")) == 2:
            return "已选中", True
        if payload.get("score") not in (None, ""):
            return "已选中", True
        return "未选中", False
    return "未知", None


def attendance_signed(payload: Mapping[str, Any]) -> bool:
    return bool(
        any(_as_int(payload.get(key)) == 1 for key in ("personStatus", "isSign", "isAttendance"))
        or _as_int(payload.get("status")) in {1, 2, 3, 4, 5, 6}
        or "已签到" in str(payload.get("personStatusName", ""))
        or "已签到" in str(payload.get("statusName", ""))
    )


def _mark_attendance_signed(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Apply the normal signed-state fields to a local activity snapshot.

    The attendance endpoint is the source of truth.  This helper only updates
    the in-memory snapshot after that endpoint accepts a request, so a caller
    can immediately render the same ``已签到`` state while the activity list
    catches up asynchronously.
    """
    signed = {
        "personStatus": 1,
        "isSign": 1,
        "isAttendance": 1,
        "personStatusName": "已签到",
        "statusName": "已签到",
        "status": 1,
        "attendanceState": "已签到",
        "_localAttendanceSigned": True,
    }
    if isinstance(payload, MutableMapping):
        payload.update(signed)
        return dict(payload)
    result = dict(payload)
    result.update(signed)
    return result


def normalize_selection(
    payload: Mapping[str, Any], *, course_id: str = "", course_name: str = ""
) -> SelectionActivity:
    if _as_int(payload.get("relationType")) != 4:
        raise ValueError("不是选人活动")
    selected_text, selected = selection_state(payload)
    start_time, end_time = _activity_times(payload)
    return SelectionActivity(
        course_id=course_id,
        course_name=course_name,
        classroom_id=str(payload.get("_roomId", payload.get("classroomId", ""))),
        classroom_title=str(payload.get("_roomTitle", payload.get("roomTitle", ""))),
        activity_id=str(payload.get("relationId", payload.get("id", ""))),
        title=str(payload.get("title", payload.get("name", ""))),
        status_code=payload.get("status"),
        status=activity_status(payload),
        time_display=str(payload.get("timeDisplay")) if payload.get("timeDisplay") else None,
        start_time=start_time,
        end_time=end_time,
        score=payload.get("score"),
        person_status=payload.get("personStatus"),
        time_status=payload.get("timeStatus"),
        selection_state=selected_text,
        selected=selected,
    )


def _active_user(data: Mapping[str, Any]) -> Mapping[str, Any]:
    users = data.get("users")
    if not isinstance(users, Mapping):
        return data
    active_name = data.get("activeUser")
    active = users.get(active_name)
    if isinstance(active, Mapping):
        return active
    for user in users.values():
        if isinstance(user, Mapping):
            return user
    return {}


def load_config(path: Path) -> AppConfig:
    if not path.exists():
        return AppConfig()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"无法读取配置文件: {path}") from exc
    if not isinstance(data, Mapping):
        raise ConfigError("配置文件必须是 JSON 对象")

    user = _active_user(data)
    active_name = str(data.get("activeUser", data.get("username", "")))
    if not active_name and isinstance(data.get("users"), Mapping):
        active_name = next((str(key) for key in data["users"]), "")
    header_value = user.get("headers")
    header_token = header_value.get("AUTHORIZATION") if isinstance(header_value, Mapping) else ""
    token = (
        data.get("auth_token")
        or data.get("auth")
        or user.get("auth")
        or user.get("AUTHORIZATION")
        or header_token
        or ""
    )
    defaults = AppConfig()
    default_location = _location_from_mapping(data) or _location_from_mapping(user) or defaults.default_location
    default_coord_system = _normalize_coord_system(
        data.get(
            "default_coord_system",
            data.get("coord_system", user.get("default_coord_system", user.get("coord_system", ""))),
        ),
        fallback=defaults.default_coord_system,
    )
    return AppConfig(
        version=_as_int(data.get("version"), 1) or 1,
        lms_base_url=str(data.get("lms_base_url", defaults.lms_base_url)),
        application_base_url=str(
            data.get("application_base_url", defaults.application_base_url)
        ),
        auth_token=str(token),
        user_id=_as_int(data.get("user_id", data.get("userID", user.get("userID", 0))), 0) or 0,
        role_id=_as_int(data.get("role_id", data.get("roleId", user.get("roleId", 0))), 0) or 0,
        timeout=_as_float(data.get("timeout"), defaults.timeout),
        retries=max(0, _as_int(data.get("retries"), defaults.retries) or 0),
        backoff=max(0.0, _as_float(data.get("backoff"), defaults.backoff)),
        activity_page_size=max(
            1, _as_int(data.get("activity_page_size"), defaults.activity_page_size) or defaults.activity_page_size
        ),
        verify_delay=max(0.0, _as_float(data.get("verify_delay"), defaults.verify_delay)),
        history_file=str(data.get("history_file", defaults.history_file)),
        login_name=str(data.get("username", data.get("loginName", active_name))),
        login_password=str(data.get("password", user.get("password", ""))),
        default_location=str(default_location),
        default_address=str(
            data.get("default_address", data.get("address", user.get("default_address", user.get("address", ""))))
        ),
        default_coord_system=default_coord_system,
    )


def resolve_token(config: AppConfig, override: str | None = None) -> str:
    token = override or os.getenv("ULEARNING_AUTHORIZATION") or config.auth_token
    if not token:
        raise AuthenticationError(
            "没有可用的 AUTHORIZATION，请使用 --token、环境变量或配置文件中的 auth。"
        )
    return token


@dataclass(frozen=True, slots=True)
class LoginResult:
    token: str
    user_id: int
    role_id: int


class LoginService:
    """Obtain the official AUTHORIZATION cookie using supplied credentials."""

    LOGIN_PATH = "/appapi/user/login/app"
    FALLBACK_PATH = "/appapi/user/check?alias=application"
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
    )

    def __init__(self, base_url: str, *, timeout: float, session: requests.Session | None = None) -> None:
        self.base_url = base_url
        self.timeout = timeout
        self.session = session or requests.Session()

    @staticmethod
    def _cookie(response: requests.Response, name: str) -> str:
        try:
            value = response.cookies.get(name)
        except requests.exceptions.CookieConflictError:
            value = response.cookies.get(name, domain=response.url.split("/")[2])
        if value:
            return str(value)
        raw = response.headers.get("Set-Cookie", "")
        if raw:
            parsed = SimpleCookie(raw)
            morsel = parsed.get(name)
            if morsel is not None:
                return morsel.value
        return ""

    @classmethod
    def _userinfo(cls, response: requests.Response) -> tuple[int, int]:
        raw = cls._cookie(response, "USERINFO")
        if not raw:
            raise AuthenticationError("登录响应缺少 USERINFO，无法确定用户身份")
        try:
            value = json.loads(unquote(raw))
            return int(value["userId"]), int(value["roleId"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AuthenticationError("登录响应中的 USERINFO 无法解析") from exc

    def login(self, username: str, password: str) -> LoginResult:
        username = username.strip()
        if not username or not password:
            raise AuthenticationError("自动登录需要账号和密码")
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": self.USER_AGENT,
        }
        response = self.session.post(
            _join_url(self.base_url, self.LOGIN_PATH),
            data={"loginName": username, "password": password, "alias": "application"},
            headers=headers,
            allow_redirects=False,
            timeout=self.timeout,
        )
        if not self._cookie(response, "AUTHORIZATION"):
            response = self.session.post(
                _join_url(self.base_url, self.FALLBACK_PATH),
                data={"loginName": username, "password": password},
                headers=headers,
                allow_redirects=False,
                timeout=self.timeout,
            )
        token = self._cookie(response, "AUTHORIZATION")
        if not token:
            raise AuthenticationError(
                f"自动登录失败（HTTP {response.status_code}），请检查账号密码或网页验证码"
            )
        user_id, role_id = self._userinfo(response)
        return LoginResult(token=token, user_id=user_id, role_id=role_id)


def save_auth_state(path: Path, *, username: str, token: str, user_id: int, role_id: int) -> None:
    """Update only auth metadata while preserving the existing config layout."""
    try:
        data: dict[str, Any] = {}
        if path.exists():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, Mapping):
                data = dict(loaded)
        users = data.get("users")
        if isinstance(users, Mapping):
            updated_users = dict(users)
            account = dict(updated_users.get(username, {}))
            account.update(auth=token, userID=user_id, roleId=role_id)
            updated_users[username] = account
            data["users"] = updated_users
            data["activeUser"] = username
        else:
            data.update(username=username, auth=token, userID=user_id, roleId=role_id)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        LOGGER.warning("cannot save refreshed auth metadata: %s", exc)


class HttpClient:
    def __init__(
        self,
        *,
        token: str,
        timeout: float,
        retries: int,
        backoff: float,
        session: requests.Session | None = None,
    ) -> None:
        self.timeout = timeout
        self.retries = max(0, retries)
        self.backoff = max(0.0, backoff)
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "AUTHORIZATION": token,
                "Accept": "application/json",
                "version": "1",
                "User-Agent": "ulearning-assistant/2.0",
            }
        )

    def get_json(self, url: str, *, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        LOGGER.debug("GET %s params=%s", url, dict(params or {}))
        for attempt in range(self.retries + 1):
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                if attempt >= self.retries:
                    raise ApiError(f"请求失败: {url}") from exc
                self._sleep(attempt, None)
                continue

            if response.status_code in (401, 403):
                raise AuthenticationError(
                    "认证已失效或当前账号无权访问该资源",
                    status_code=response.status_code,
                )
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < self.retries:
                    self._sleep(attempt, response.headers.get("Retry-After"))
                    continue
            try:
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as exc:
                raise ApiError(f"接口响应无效: {url}", status_code=response.status_code) from exc
            if not isinstance(payload, dict):
                raise SchemaError(f"接口返回值不是 JSON 对象: {url}")
            return payload
        raise ApiError(f"请求重试耗尽: {url}")

    def post_json(self, url: str, *, json_body: Mapping[str, Any]) -> dict[str, Any]:
        """Send one explicit write request; POST is intentionally not retried."""
        LOGGER.debug("POST %s fields=%s", url, sorted(json_body.keys()))
        try:
            response = self.session.post(url, json=dict(json_body), timeout=self.timeout)
        except requests.RequestException as exc:
            raise ApiError(f"请求失败: {url}") from exc

        if response.status_code in (401, 403):
            raise AuthenticationError(
                "认证已失效或当前账号无权访问该资源",
                status_code=response.status_code,
            )
        try:
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise ApiError(f"接口响应无效: {url}", status_code=response.status_code) from exc
        if not isinstance(payload, dict):
            raise SchemaError(f"接口返回值不是 JSON 对象: {url}")
        return payload

    def _sleep(self, attempt: int, retry_after: str | None) -> None:
        delay = self.backoff * (2**attempt)
        try:
            if retry_after is not None:
                delay = max(delay, float(retry_after))
        except ValueError:
            pass
        LOGGER.debug("retry in %.2fs", delay)
        if delay:
            time.sleep(delay)


class CourseService:
    def __init__(self, client: HttpClient, base_url: str) -> None:
        self.client = client
        self.base_url = base_url

    def list_courses(self) -> list[Course]:
        page_size = 100
        courses: list[Course] = []
        page = 1
        total: int | None = None
        while True:
            payload = self.client.get_json(
                _join_url(self.base_url, "/courseapi/courses/students"),
                params={
                    "keyword": "",
                    "publishStatus": 1,
                    "type": 1,
                    "pn": page,
                    "ps": page_size,
                    "lang": "zh",
                },
            )
            items = payload.get("courseList")
            if not isinstance(items, list):
                raise SchemaError("课程接口缺少 courseList 数组")
            courses.extend(
                Course.from_payload(item) for item in items if isinstance(item, Mapping)
            )
            if total is None:
                total = _as_int(payload.get("total"))
            if not items or (total is not None and len(courses) >= total) or len(items) < page_size:
                break
            page += 1
        return courses


def _selection_public_fields(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return server fields only; omit this program's internal _* metadata."""
    return {str(key): value for key, value in payload.items() if not str(key).startswith("_")}


def _diff_json(before: Any, after: Any, *, path: str = "$") -> list[dict[str, Any]]:
    """Recursively compare two JSON-like values and return changed paths."""
    changes: list[dict[str, Any]] = []
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        keys = sorted(set(before) | set(after), key=str)
        for key in keys:
            child = f"{path}.{key}"
            if key not in before:
                changes.append({"path": child, "kind": "added", "before": None, "after": after[key]})
            elif key not in after:
                changes.append({"path": child, "kind": "removed", "before": before[key], "after": None})
            else:
                changes.extend(_diff_json(before[key], after[key], path=child))
        return changes
    if isinstance(before, list) and isinstance(after, list):
        if before != after:
            changes.append({"path": path, "kind": "changed", "before": before, "after": after})
        return changes
    if before != after:
        changes.append({"path": path, "kind": "changed", "before": before, "after": after})
    return changes


def build_selection_evidence(
    course: Course,
    activity: Mapping[str, Any],
    *,
    classroom_id: str = "",
    activity_service: "ActivityService",
) -> dict[str, Any]:
    """Collect extra read-only server data to understand selection semantics.

    This deliberately performs GET requests only. It never changes the activity,
    score, personStatus, or any other server-side selection state.
    """
    activity_id = str(activity.get("relationId", activity.get("id", "")))
    if not activity_id:
        raise ApiError("选人活动缺少 relationId")
    if _as_int(activity.get("relationType")) != 4:
        raise ApiError("目标活动不是 relationType=4 的选人活动")

    fresh = next(
        (dict(item) for item in activity_service.list_activities(course)
         if str(item.get("relationId", item.get("id", ""))) == activity_id),
        None,
    )
    if fresh is None:
        raise ApiError(f"课程 {course.course_id} 中未找到选人活动 {activity_id}")

    resolved_room = str(
        classroom_id
        or fresh.get("classroomId", "")
        or fresh.get("_roomId", "")
        or ""
    )
    if not resolved_room:
        resolved_room = activity_service.resolve_classroom_id(course, activity_id)

    detail_endpoint = _join_url(
        activity_service.lms_base_url,
        "/classroomapi/wisdomClassroom/student/scoreDetailForClassroom",
    )
    detail_payload = activity_service.client.get_json(
        detail_endpoint,
        params={"type": 2, "classroomId": resolved_room, "lang": "zh"},
    )
    detail_result = detail_payload.get("result")
    detail_items = detail_result.get("list") if isinstance(detail_result, Mapping) else None
    if not isinstance(detail_items, list):
        raise SchemaError("选人详情接口缺少 result.list 数组")

    matches = [
        dict(item)
        for item in detail_items
        if isinstance(item, Mapping)
        and str(item.get("relationId", item.get("id", ""))) == activity_id
    ]

    evidence: dict[str, Any] = {
        "captured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "course": {
            "course_id": course.course_id,
            "course_name": course.name,
            "class_id": course.class_id,
            "class_name": course.class_name,
        },
        "activity_id": activity_id,
        "relation_type": _as_int(fresh.get("relationType")),
        "student_activity_endpoint": _join_url(
            activity_service.lms_base_url,
            f"/courseapi/classActivity/stu/{course.course_id}/-1",
        ),
        "student_activity": _selection_public_fields(fresh),
        "classroom_id": resolved_room,
        "classroom_detail_endpoint": detail_endpoint,
        "classroom_detail_count": len(detail_items),
        "classroom_detail_result_meta": (
            {str(key): value for key, value in detail_result.items() if key != "list"}
            if isinstance(detail_result, Mapping)
            else {}
        ),
        "classroom_matches": [_selection_public_fields(item) for item in matches],
    }

    if matches:
        student_public = _selection_public_fields(fresh)
        classroom_public = _selection_public_fields(matches[0])
        evidence["student_vs_classroom_diff"] = _diff_json(student_public, classroom_public)
        evidence["classroom_only_fields"] = sorted(
            set(classroom_public) - set(student_public), key=str
        )
        evidence["student_only_fields"] = sorted(
            set(student_public) - set(classroom_public), key=str
        )
    else:
        evidence["student_vs_classroom_diff"] = []
        evidence["classroom_only_fields"] = []
        evidence["student_only_fields"] = []

    return evidence


class ActivityService:
    def __init__(
        self,
        client: HttpClient,
        *,
        application_base_url: str,
        lms_base_url: str,
        user_id: int,
        page_size: int = 100,
    ) -> None:
        self.client = client
        self.application_base_url = application_base_url
        self.lms_base_url = lms_base_url
        self.user_id = user_id
        self.page_size = max(1, page_size)

    def list_activities(self, course: Course | str, *, course_name: str = "") -> list[dict[str, Any]]:
        course_id = course.course_id if isinstance(course, Course) else str(course)
        if isinstance(course, Course):
            course_name = course.name
        url = _join_url(self.lms_base_url, f"/courseapi/classActivity/stu/{course_id}/-1")
        activities: list[dict[str, Any]] = []
        page = 1
        total: int | None = None
        while True:
            payload = self.client.get_json(
                url,
                params={"pn": page, "ps": self.page_size, "lang": "zh"},
            )
            items = payload.get("list")
            if not isinstance(items, list):
                raise SchemaError("官方活动接口缺少 list 数组")
            if total is None:
                total = _as_int(payload.get("total"))
            for activity in items:
                if not isinstance(activity, Mapping):
                    continue
                enriched = dict(activity)
                enriched.update(
                    {
                        "_courseId": course_id,
                        "_courseName": course_name,
                        "_roomId": str(activity.get("classroomId", "") or ""),
                    }
                )
                activities.append(enriched)
            if not items or (total is not None and len(activities) >= total) or len(items) < self.page_size:
                break
            page += 1
        return activities

    def resolve_classroom_id(
        self, course: Course | str, activity_id: str, *, preferred: str = ""
    ) -> str:
        """Resolve the classroom ID required by the legacy attendance POST.

        The official activity list can omit classroomId for student accounts.
        This lookup is only used immediately before an explicit attendance
        request, so the normal activity query still follows the official API.
        """
        if preferred:
            return str(preferred)
        course_id = course.course_id if isinstance(course, Course) else str(course)
        rooms_payload = self.client.get_json(
            _join_url(
                self.application_base_url,
                "/classroomapi/wisdomClassroom/student/getClassroomList",
            ),
            params={
                "ocId": course_id,
                "pageSize": 999,
                "pageNum": 1,
                "teacherId": self.user_id,
                "order": 0,
                "type": 1,
            },
        )
        result = rooms_payload.get("result")
        rooms = result.get("list") if isinstance(result, Mapping) else None
        if not isinstance(rooms, list):
            raise SchemaError("课堂接口缺少 result.list 数组")

        room_list = [room for room in rooms if isinstance(room, Mapping) and room.get("id")]
        room_list.sort(key=lambda room: 0 if _as_int(room.get("status")) == 0 else 1)
        for room in room_list:
            detail_payload = self.client.get_json(
                _join_url(
                    self.lms_base_url,
                    "/classroomapi/wisdomClassroom/student/scoreDetailForClassroom",
                ),
                params={"type": 2, "classroomId": room["id"], "lang": "zh"},
            )
            detail_result = detail_payload.get("result")
            items = detail_result.get("list") if isinstance(detail_result, Mapping) else None
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, Mapping) and str(item.get("relationId")) == str(activity_id):
                    return str(room["id"])
        raise ApiError(f"未能为活动 {activity_id} 找到课堂 ID，请手动填写 --classroom-id")

    def list_selection(
        self,
        course: Course | str,
        *,
        course_name: str = "",
        active_only: bool = False,
    ) -> list[SelectionActivity]:
        activities = self.list_activities(course, course_name=course_name)
        result: list[SelectionActivity] = []
        for activity in activities:
            if _as_int(activity.get("relationType")) != 4:
                continue
            item = normalize_selection(
                activity,
                course_id=str(activity.get("_courseId", "")),
                course_name=str(activity.get("_courseName", course_name)),
            )
            if active_only and item.status != "进行中":
                continue
            result.append(item)
        return result


class AttendanceService:
    """Perform one user-initiated attendance request and verify its result.

    ``allow_ended`` defaults to ``True``.  The activity window is a useful
    display hint, but the sign endpoint remains the authority on whether a
    late request is accepted.  Keeping the request path available after the
    window closes also supports deployments that permit teacher-configured
    make-up attendance.
    """

    SIGN_URL_PATH = "/classroomapi/newAttendance/signByStu"
    # The teacher classroom uses this endpoint when an attendance record is
    # edited after the roll call has been closed.  Unlike ``signByStu`` it
    # writes the existing student record (status=1 means present/normal).
    TEACHER_STATUS_URL_PATH = "/classroomapi/newAttendance/signByTea"

    def __init__(
        self,
        client: HttpClient,
        activity_service: ActivityService,
        *,
        application_base_url: str,
        user_id: int,
        teacher_client: HttpClient | None = None,
        teacher_user_id: int = 0,
        verify_delay: float = 1.5,
        history_file: Path | None = None,
        allow_ended: bool = True,
    ) -> None:
        self.client = client
        self.activity_service = activity_service
        self.application_base_url = application_base_url
        self.user_id = user_id
        # A student session must never be promoted locally. A separate
        # server-issued teacher/admin session is required for signByTea.
        self.teacher_client = teacher_client
        self.teacher_user_id = int(teacher_user_id or 0)
        self.verify_delay = max(0.0, verify_delay)
        self.history_file = history_file
        self.allow_ended = bool(allow_ended)
        self._local_signed_ids = self._load_local_signed_ids()

    @staticmethod
    def can_sign(
        activity: Mapping[str, Any], *, allow_ended: bool = True
    ) -> tuple[bool, str]:
        if _as_int(activity.get("relationType")) != 1:
            return False, "不是点名活动"
        if attendance_signed(activity):
            return False, "已签到"
        status = activity_status(activity)
        if status == "进行中":
            return True, "进行中，可签到"
        if allow_ended and activity_is_ended(activity):
            return True, "已结束，尝试补签"
        return False, f"活动状态为 {status}"

    @staticmethod
    def _activity_id(activity: Mapping[str, Any]) -> str:
        return str(activity.get("relationId", activity.get("id", "")))

    def _load_local_signed_ids(self) -> set[str]:
        """Load successful attempts recorded by an earlier process.

        The history file is optional.  Reading only entries with
        ``success=true`` gives the UI a short-lived optimistic overlay when a
        server list endpoint lags behind a successful POST.
        """
        if self.history_file is None or not self.history_file.exists():
            return set()
        try:
            value = json.loads(self.history_file.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return set()
        if not isinstance(value, list):
            return set()
        return {
            str(item.get("activity_id"))
            for item in value
            if isinstance(item, Mapping)
            and item.get("success") is True
            and item.get("activity_id") not in (None, "")
        }

    def is_locally_signed(self, activity: Mapping[str, Any]) -> bool:
        """Return whether this process has accepted a sign request for it."""
        return self._activity_id(activity) in self._local_signed_ids or bool(
            activity.get("_localAttendanceSigned")
        )

    def is_signed(self, activity: Mapping[str, Any]) -> bool:
        """Use only server fields as the displayed attendance state."""
        return attendance_signed(activity)

    def _remember_signed(self, activity: Mapping[str, Any]) -> dict[str, Any]:
        activity_id = self._activity_id(activity)
        if activity_id:
            self._local_signed_ids.add(activity_id)
        return _mark_attendance_signed(activity)

    @staticmethod
    def _find_activity(activities: Iterable[Mapping[str, Any]], activity_id: str) -> dict[str, Any] | None:
        for activity in activities:
            if str(activity.get("relationId", activity.get("id", ""))) == str(activity_id):
                return dict(activity)
        return None

    @staticmethod
    def _message(response: Mapping[str, Any]) -> str:
        value = _first(response, ("message", "msg", "error"))
        return str(value) if value is not None else ""

    @classmethod
    def _response_summary(cls, response: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "code": response.get("code"),
            "success": response.get("success"),
            "message": cls._message(response),
        }

    @classmethod
    def _response_accepted(cls, response: Mapping[str, Any]) -> bool:
        """Require an affirmative server result; missing fields are not success."""
        code = _as_int(response.get("code"))
        success = response.get("success")
        message = cls._message(response).strip().lower()
        negative = any(
            mark in message
            for mark in (
                "失败",
                "结束",
                "缺勤",
                "无权",
                "权限",
                "距离老师太远",
                "定位不准确",
                "forbidden",
                "denied",
                "error",
                "fail",
            )
        )
        if negative or success is False or (code is not None and code != 0):
            return False
        return success is True or code == 0 or any(
            mark in message for mark in ("成功", "已签到", "success", "ok")
        )

    @staticmethod
    def _server_attendance_record(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """Unwrap the record returned by different deployed API versions."""
        current: Mapping[str, Any] = payload
        for _ in range(3):
            nested = next(
                (
                    current.get(key)
                    for key in ("data", "result", "attendance", "record")
                    if isinstance(current.get(key), Mapping)
                ),
                None,
            )
            if not isinstance(nested, Mapping):
                break
            current = nested
        return current

    def _sign_once(
        self,
        course: Course | str,
        activity: Mapping[str, Any],
        *,
        classroom_id: str = "",
        location: str = "",
        address: str = "",
        coord_system: str = "gcj02",
        enter_way: int = 1,
        attendance_code: str = "",
        dry_run: bool = False,
        allow_ended: bool | None = None,
    ) -> dict[str, Any]:
        course_id = course.course_id if isinstance(course, Course) else str(course)
        activity_id = str(activity.get("relationId", activity.get("id", "")))
        if not activity_id:
            raise ApiError("签到活动缺少 relationId")
        if self.user_id <= 0:
            raise ConfigError("配置文件缺少有效 user_id，无法构造签到请求")

        fresh = self._find_activity(self.activity_service.list_activities(course), activity_id)
        if fresh is None:
            raise ApiError(f"课程 {course_id} 中未找到活动 {activity_id}")
        if self.is_signed(fresh):
            # A previous accepted request may not be visible in the activity
            # list yet.  Preserve the normal idempotent behaviour locally.
            self._remember_signed(fresh)
            self._remember_signed(activity)
            return {
                "success": True,
                "message": "已签到",
                "activity_id": activity_id,
                "dry_run": dry_run,
                "attendance_state": "已签到",
            }

        can_sign, reason = self.can_sign(
            fresh,
            allow_ended=self.allow_ended if allow_ended is None else bool(allow_ended),
        )
        if not can_sign:
            return {
                "success": reason == "已签到",
                "message": reason,
                "activity_id": activity_id,
                "dry_run": dry_run,
            }

        resolved_classroom_id = str(
            classroom_id
            or fresh.get("classroomId", "")
            or fresh.get("_roomId", "")
            or ""
        )
        if not resolved_classroom_id:
            resolved_classroom_id = self.activity_service.resolve_classroom_id(
                course, activity_id
            )

        # A closed roll call already has a persisted ``status=0`` row.  The
        # student endpoint cannot rewrite that row; use the same teacher-side
        # write used by the official attendance-record page instead.
        if activity_is_ended(fresh):
            teacher_payload = self._teacher_status_payload(
                fresh, classroom_id=resolved_classroom_id
            )
            if dry_run:
                return {
                    "success": False,
                    "activity_id": activity_id,
                    "classroom_id": resolved_classroom_id,
                    "dry_run": True,
                    "request": teacher_payload,
                    "write_analysis": self.teacher_write_analysis(
                        activity_id=activity_id,
                        classroom_id=resolved_classroom_id,
                        student_user_id=self.user_id,
                        teacher_user_id=self.teacher_user_id,
                    ),
                    "message": "dry-run：不会发送服务端补签请求",
                }
            if self.teacher_client is None:
                return {
                    "success": False,
                    "activity_id": activity_id,
                    "classroom_id": resolved_classroom_id,
                    "dry_run": False,
                    "request": teacher_payload,
                    "write_analysis": self.teacher_write_analysis(
                        activity_id=activity_id,
                        classroom_id=resolved_classroom_id,
                        student_user_id=self.user_id,
                        teacher_user_id=self.teacher_user_id,
                    ),
                    "authorization_required": True,
                    "message": (
                        "student token cannot rewrite an ended attendance record; "
                        "provide a separately server-issued teacher/admin token"
                    ),
                }
            status_payload, status_response = self._update_ended_server_status(
                fresh, classroom_id=resolved_classroom_id
            )
            status_summary = self._response_summary(status_response)
            result: dict[str, Any] = {
                "success": False,
                "activity_id": activity_id,
                "classroom_id": resolved_classroom_id,
                "dry_run": dry_run,
                "request": status_payload,
                "response": status_summary,
            }
            message = status_summary["message"]
            accepted = self._response_accepted(status_response)
            if not accepted:
                result["message"] = message or "服务端补签接口返回失败"
                return result

            # Read the record back from the server.  Do not turn this into a
            # local-only success: the returned status must be 1 (normal).
            if self.verify_delay:
                time.sleep(self.verify_delay)
            try:
                server_state = self.client.get_json(
                    _join_url(
                        self.activity_service.lms_base_url,
                        f"/apps/newAttendance/getAttendanceForStu/{activity_id}/{self.user_id}",
                    )
                )
            except ApiError:
                server_state = {}
            server_record = self._server_attendance_record(server_state)
            normal_fields = {
                key: _as_int(server_record.get(key))
                for key in ("status", "personStatus", "isSign", "isAttendance")
                if server_record.get(key) is not None
            }
            if any(value == 1 for value in normal_fields.values()):
                self._remember_signed(fresh)
                self._remember_signed(activity)
                result.update(
                    success=True,
                    message="补签成功，服务端状态已更新为正常",
                    attendance_state="已签到",
                    server_record=dict(server_record),
                )
            else:
                result.update(
                    message="服务端回读仍为缺勤，补签未生效",
                    attendance_state=(
                        "缺勤"
                        if any(value == 0 for value in normal_fields.values())
                        else "状态未知"
                    ),
                    server_record=dict(server_record),
                )
            return result

        prepared_location, location_diagnostic = _prepare_location(location, coord_system)
        payload = {
            "attendanceID": activity_id,
            "classID": resolved_classroom_id,
            "userID": self.user_id,
            "location": prepared_location,
            "address": address.strip(),
            "enterWay": int(enter_way),
            "attendanceCode": attendance_code.strip(),
        }
        if not dry_run and not any(
            (payload["location"], payload["address"], payload["attendanceCode"])
        ):
            raise ConfigError("请提供真实位置、位置文字或教师提供的签到码")
        result: dict[str, Any] = {
            "success": False,
            "activity_id": activity_id,
            "classroom_id": resolved_classroom_id,
            "dry_run": dry_run,
            "request": payload,
        }
        if location_diagnostic is not None:
            result["location_diagnostic"] = location_diagnostic
        if dry_run:
            result["message"] = "dry-run：未发送请求"
            return result

        response = self.client.post_json(
            _join_url(self.application_base_url, self.SIGN_URL_PATH),
            json_body=payload,
        )
        response_summary = self._response_summary(response)
        result["response"] = response_summary
        message = response_summary["message"]
        if not self._response_accepted(response):
            if "已经签过" in message or "已签到" in message:
                result.update(
                    success=True,
                    message="已签到",
                    attendance_state="已签到",
                )
                self._remember_signed(fresh)
                self._remember_signed(activity)
                return result
            result["message"] = message or "签到接口返回失败"
            failure_diagnostic = _location_failure_diagnostic(message, location_diagnostic)
            if failure_diagnostic is not None:
                result["location_diagnostic"] = failure_diagnostic
            return result

        # A successful POST is authoritative for this request.  Mark both
        # snapshots immediately; the follow-up GET can otherwise still carry
        # the old unsigned/ended representation for a short period.
        self._remember_signed(fresh)
        self._remember_signed(activity)
        result["attendance_state"] = "已签到"

        if self.verify_delay:
            time.sleep(self.verify_delay)
        try:
            verified_activities = self.activity_service.list_activities(course)
        except ApiError:
            verified_activities = []
        verified = self._find_activity(verified_activities, activity_id)
        if verified is not None and self.is_signed(verified):
            result.update(success=True, message="签到成功")
        else:
            # Some deployments update the activity list asynchronously. Keep
            # the original per-attendance verification as a fallback while
            # retaining the accepted POST as the immediate local result.
            try:
                status_payload = self.client.get_json(
                    _join_url(
                        self.activity_service.lms_base_url,
                        f"/apps/newAttendance/getAttendanceForStu/{activity_id}/{self.user_id}",
                    )
                )
            except ApiError:
                status_payload = {}
            if attendance_signed(status_payload) or self.is_locally_signed(status_payload):
                result.update(success=True, message="签到成功")
            else:
                # The endpoint accepted the write, while the read model is
                # still stale.  Keep the local state normal and report the
                # accepted request instead of turning a successful POST into
                # a failure solely because of propagation delay.
                result.update(success=True, message="签到成功（状态同步中）")
        return result

    def _update_ended_server_status(
        self, activity: Mapping[str, Any], *, classroom_id: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Persist an ended attendance record as present on the server."""
        if self.teacher_client is None:
            raise AuthenticationError(
                "a separately authenticated teacher/admin client is required for signByTea"
            )
        payload = self._teacher_status_payload(activity, classroom_id=classroom_id)
        response = self.teacher_client.post_json(
            _join_url(self.application_base_url, self.TEACHER_STATUS_URL_PATH),
            json_body=payload,
        )
        return payload, response

    def _teacher_status_payload(
        self, activity: Mapping[str, Any], *, classroom_id: str
    ) -> dict[str, Any]:
        """Build, but do not send, the teacher-side attendance write payload."""
        activity_id = self._activity_id(activity)
        return {
            "attendanceID": activity_id,
            "attendanceId": activity_id,
            "classID": str(classroom_id),
            "classId": str(classroom_id),
            # userID identifies the student row to update; authorization comes
            # from the separate teacher_client session above.
            "userID": self.user_id,
            "userId": self.user_id,
            "status": 1,
            "personStatus": 1,
            "signStatus": 1,
        }

    @classmethod
    def teacher_write_analysis(
        cls,
        *,
        activity_id: str,
        classroom_id: str,
        student_user_id: int,
        teacher_user_id: int = 0,
    ) -> dict[str, Any]:
        """Return a redacted, read-only description of the ended-record write."""
        return {
            "endpoint_path": cls.TEACHER_STATUS_URL_PATH,
            "method": "POST",
            "required_authority": "server-issued teacher/admin session",
            "student_user_id": int(student_user_id),
            "teacher_user_id": int(teacher_user_id or 0),
            "payload_fields": {
                "attendanceID": str(activity_id),
                "classID": str(classroom_id),
                "userID": "student_user_id",
                "status": 1,
                "personStatus": 1,
                "signStatus": 1,
            },
            "student_path": cls.SIGN_URL_PATH,
            "student_path_effect": "cannot rewrite an already persisted ended/absent row",
        }

    def _record_history(self, result: Mapping[str, Any]) -> None:
        if result.get("success") is True:
            activity_id = str(result.get("activity_id", ""))
            if activity_id:
                self._local_signed_ids.add(activity_id)
        if self.history_file is None:
            return
        try:
            entries: list[Any] = []
            if self.history_file.exists():
                old = json.loads(self.history_file.read_text(encoding="utf-8"))
                if isinstance(old, list):
                    entries = old
            entries.append(
                {
                    "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "activity_id": result.get("activity_id"),
                    "classroom_id": result.get("classroom_id"),
                    "success": result.get("success"),
                    "attendance_state": result.get("attendance_state"),
                    "dry_run": result.get("dry_run", False),
                    "message": result.get("message"),
                    "response": result.get("response"),
                }
            )
            self.history_file.parent.mkdir(parents=True, exist_ok=True)
            self.history_file.write_text(
                json.dumps(entries[-500:], ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except (OSError, ValueError, TypeError) as exc:
            LOGGER.warning("cannot save attendance history: %s", exc)

    def sign(
        self,
        course: Course | str,
        activity: Mapping[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        try:
            result = self._sign_once(course, activity, **kwargs)
        except Exception as exc:
            self._record_history(
                {
                    "activity_id": activity.get("relationId", activity.get("id", "")),
                    "success": False,
                    "message": str(exc),
                    "dry_run": bool(kwargs.get("dry_run", False)),
                }
            )
            raise
        self._record_history(result)
        return result


def _make_services(
    config_path: Path, token_override: str | None = None
) -> tuple[AppConfig, HttpClient, CourseService, ActivityService]:
    config = load_config(config_path)
    session = requests.Session()
    token = token_override or os.getenv("ULEARNING_AUTHORIZATION") or config.auth_token
    user_id = config.user_id
    role_id = config.role_id
    # Prefer a fresh official login whenever credentials are available. This
    # keeps an expired AUTHORIZATION in the config from blocking normal use.
    if config.login_name and config.login_password and not token_override:
        login_result = LoginService(
            config.application_base_url,
            timeout=config.timeout,
            session=session,
        ).login(config.login_name, config.login_password)
        token = login_result.token
        user_id = login_result.user_id
        role_id = login_result.role_id
        save_auth_state(
            config_path,
            username=config.login_name,
            token=token,
            user_id=user_id,
            role_id=role_id,
        )
        config.auth_token = token
        config.user_id = user_id
        config.role_id = role_id
    if not token:
        token = resolve_token(config, token_override)
    client = HttpClient(
        token=token,
        timeout=config.timeout,
        retries=config.retries,
        backoff=config.backoff,
        session=session,
    )
    return (
        config,
        client,
        CourseService(client, config.lms_base_url),
        ActivityService(
            client,
            application_base_url=config.application_base_url,
            lms_base_url=config.lms_base_url,
            user_id=user_id,
            page_size=config.activity_page_size,
        ),
    )


def _resolve_config_path(config_path: Path, configured: str) -> Path:
    path = Path(configured)
    return path if path.is_absolute() else config_path.parent / path


def _activity_rows(
    activities: Iterable[Mapping[str, Any]],
    *,
    signed_checker: Callable[[Mapping[str, Any]], bool] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for activity in activities:
        relation_type = _as_int(activity.get("relationType"), -1) or -1
        start_time, end_time = _activity_times(activity)
        row: dict[str, Any] = {
            "course_id": str(activity.get("_courseId", "")),
            "course_name": str(activity.get("_courseName", "")),
            "activity_id": str(activity.get("relationId", activity.get("id", ""))),
            "relation_type": relation_type,
            "type": RELATION_TYPE.get(relation_type, f"未知({relation_type})"),
            "title": str(activity.get("title", activity.get("name", ""))),
            "status": activity_status(activity),
            "time_display": activity.get("timeDisplay"),
            "start_time": start_time,
            "end_time": end_time,
            "score": activity.get("score"),
            "person_status": activity.get("personStatus"),
            "time_status": activity.get("timeStatus"),
        }
        if relation_type in (3, 4):
            row["selection_state"], row["selected"] = selection_state(activity)
        elif relation_type == 1:
            is_signed = attendance_signed(activity) or (
                signed_checker is not None and signed_checker(activity)
            )
            row["attendance_state"] = "已签到" if is_signed else "未签到"
        rows.append(row)
    return rows


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _diagnostic(course: Course, activities: Sequence[Mapping[str, Any]], *, base_url: str) -> dict[str, Any]:
    type_counts: dict[str, int] = {}
    fields: set[str] = set()
    samples: list[dict[str, Any]] = []
    for activity in activities:
        relation_type = _as_int(activity.get("relationType"), -1) or -1
        type_key = str(relation_type)
        type_counts[type_key] = type_counts.get(type_key, 0) + 1
        fields.update(key for key in activity if not key.startswith("_"))
        if len(samples) < 20:
            samples.append(
                {
                    "activity_id": activity.get("relationId"),
                    "relation_type": relation_type,
                    "title": activity.get("title"),
                    "status": activity.get("status"),
                    "time_display": activity.get("timeDisplay"),
                    "time_status": activity.get("timeStatus"),
                    "score": activity.get("score"),
                    "person_status": activity.get("personStatus"),
                    "location_field_names": _location_field_names(activity),
                }
            )
    return {
        "endpoint": _join_url(base_url, f"/courseapi/classActivity/stu/{course.course_id}/-1"),
        "course_id": course.course_id,
        "course_name": course.name,
        "activity_count": len(activities),
        "relation_type_distribution": type_counts,
        "fields": sorted(fields),
        "samples": samples,
    }


def run_cli(args: argparse.Namespace) -> int:
    if args.command == "self-test":
        item = normalize_selection(
            {
                "relationId": "test",
                "relationType": 4,
                "title": "test",
                "timeDisplay": "进行中",
                "score": 2,
            }
        )
        assert item.selected is True
        assert item.selection_state == "已选中"
        historical_selected = normalize_selection(
            {
                "relationId": 114918,
                "relationType": 4,
                "title": "2025-02-25 14:35 选人",
                "status": 2,
                "timeDisplay": "已结束",
                "timeStatus": 3,
                "score": 2,
                "personStatus": 2,
            }
        )
        assert historical_selected.selected is True
        assert historical_selected.selection_state == "已选中"
        not_selected = normalize_selection(
            {"relationId": "test-2", "relationType": 4, "timeDisplay": "进行中"}
        )
        assert not_selected.selected is False
        assert not_selected.selection_state == "未选中"
        pending = normalize_selection(
            {
                "relationId": "test-3",
                "relationType": 4,
                "timeDisplay": "进行中",
                "selectionStatus": "待确认",
            }
        )
        assert pending.selected is None
        assert pending.selection_state == "待确认"
        assert activity_status({"timeDisplay": "已结束", "status": 0}) == "已结束"
        normalized_location, normalized_diag = _prepare_location(
            "22.901269,113.879022", "gcj02"
        )
        assert normalized_location == FIXED_LOCATION
        assert normalized_diag is not None
        assert normalized_diag["input_order"] == "lat,lon"
        wgs_location, wgs_diag = _prepare_location(FIXED_LOCATION, "wgs84")
        assert wgs_location != FIXED_LOCATION
        assert wgs_diag is not None
        assert wgs_diag["converted"] == "wgs84->gcj02"
        bd_location, bd_diag = _prepare_location(FIXED_LOCATION, "bd09")
        assert bd_location != FIXED_LOCATION
        assert bd_diag is not None
        assert bd_diag["converted"] == "bd09->gcj02"
        signable = signable_attendance_activities(
            [
                {
                    "relationId": "attendance-active",
                    "relationType": 1,
                    "timeStatus": 2,
                    "personStatus": 0,
                    "status": 0,
                },
                {
                    "relationId": "attendance-signed",
                    "relationType": 1,
                    "timeStatus": 2,
                    "personStatus": 1,
                },
                {
                    "relationId": "attendance-ended",
                    "relationType": 1,
                    "timeStatus": 3,
                    "personStatus": 0,
                    "status": 0,
                },
                {"relationId": "selection", "relationType": 4, "timeStatus": 2},
            ]
        )
        assert [item["relationId"] for item in signable] == [
            "attendance-active",
            "attendance-ended",
        ]

        class FakeLoginSession:
            def post(self, url: str, **kwargs: Any) -> requests.Response:
                response = requests.Response()
                response.status_code = 302
                response.url = url
                response.headers["Set-Cookie"] = (
                    "AUTHORIZATION=test-token; Path=/, "
                    "USERINFO=%7B%22userId%22%3A7%2C%22roleId%22%3A9%7D; Path=/"
                )
                return response

        login_result = LoginService(
            "https://application.example",
            timeout=1,
            session=FakeLoginSession(),  # type: ignore[arg-type]
        ).login("student", "secret")
        assert login_result.token == "test-token"
        assert login_result.user_id == 7
        assert login_result.role_id == 9

        class SelfTestClient:
            def __init__(self) -> None:
                self.signed = False
                self.post_body: dict[str, Any] | None = None

            def get_json(self, url: str, *, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
                return {
                    "total": 1,
                    "list": [
                        {
                            "relationId": "attendance-1",
                            "relationType": 1,
                            "title": "测试点名",
                            "timeDisplay": "进行中",
                            "timeStatus": 2,
                            "status": 1 if self.signed else 0,
                            "personStatus": 1 if self.signed else 0,
                            "classroomId": "room-1",
                        }
                    ],
                }

            def post_json(self, url: str, *, json_body: Mapping[str, Any]) -> dict[str, Any]:
                self.post_body = dict(json_body)
                self.signed = True
                return {"code": 0, "message": "ok"}

        fake_client = SelfTestClient()
        fake_activities = ActivityService(
            fake_client,
            application_base_url="https://application.example",
            lms_base_url="https://lms.example",
            user_id=7,
        )
        fake_attendance = AttendanceService(
            fake_client,
            fake_activities,
            application_base_url="https://application.example",
            user_id=7,
            verify_delay=0,
        )
        sign_result = fake_attendance.sign(
            Course("c-1", "课程", "class-1", "班级"),
            {"relationId": "attendance-1", "relationType": 1},
            location=FIXED_LOCATION,
        )
        assert sign_result["success"] is True
        assert fake_client.post_body is not None
        assert fake_client.post_body["location"] == FIXED_LOCATION

        class FailedSignSelfTestClient(SelfTestClient):
            def post_json(
                self, url: str, *, json_body: Mapping[str, Any]
            ) -> dict[str, Any]:
                self.post_body = dict(json_body)
                return {"message": "签到失败，点名已结束"}

        failed_client = FailedSignSelfTestClient()
        failed_activities = ActivityService(
            failed_client,
            application_base_url="https://application.example",
            lms_base_url="https://lms.example",
            user_id=7,
        )
        failed_attendance = AttendanceService(
            failed_client,
            failed_activities,
            application_base_url="https://application.example",
            user_id=7,
            verify_delay=0,
        )
        failed_activity = {
            "relationId": "attendance-1",
            "relationType": 1,
            "timeStatus": 2,
            "status": 0,
            "personStatus": 0,
        }
        failed_result = failed_attendance.sign(
            Course("c-1", "课程", "class-1", "班级"),
            failed_activity,
            location=FIXED_LOCATION,
        )
        assert failed_result["success"] is False
        assert failed_attendance.is_locally_signed(failed_activity) is False
        assert failed_activity["personStatus"] == 0

        class LateSelfTestClient:
            """A server fixture that accepts a late request but lags on GET."""

            def __init__(self) -> None:
                self.post_body: dict[str, Any] | None = None
                self.server_status = 0

            def get_json(
                self, url: str, *, params: Mapping[str, Any] | None = None
            ) -> dict[str, Any]:
                if "getAttendanceForStu" in url:
                    return {"status": self.server_status, "personStatus": self.server_status}
                return {
                    "total": 1,
                    "list": [
                        {
                            "relationId": "attendance-ended",
                            "relationType": 1,
                            "title": "已结束点名",
                            "timeDisplay": "已结束",
                            "timeStatus": 3,
                            "status": 0,
                            "personStatus": 0,
                            "classroomId": "room-1",
                        }
                    ],
                }

            def post_json(
                self, url: str, *, json_body: Mapping[str, Any]
            ) -> dict[str, Any]:
                self.post_body = dict(json_body)
                if "signByTea" in url:
                    self.server_status = int(json_body.get("status", 0))
                return {"code": 0, "message": "ok"}

        late_client = LateSelfTestClient()
        late_activities = ActivityService(
            late_client,
            application_base_url="https://application.example",
            lms_base_url="https://lms.example",
            user_id=7,
        )
        late_attendance = AttendanceService(
            late_client,
            late_activities,
            application_base_url="https://application.example",
            user_id=7,
            teacher_client=late_client,
            teacher_user_id=7,
            verify_delay=0,
        )
        late_activity = {
            "relationId": "attendance-ended",
            "relationType": 1,
            "timeStatus": 3,
            "personStatus": 0,
            "status": 0,
        }
        assert late_attendance.can_sign(late_activity)[0] is True
        student_only_late = AttendanceService(
            late_client,
            late_activities,
            application_base_url="https://application.example",
            user_id=7,
            verify_delay=0,
        )
        student_only_result = student_only_late.sign(
            Course("c-1", "课程", "class-1", "班级"),
            late_activity,
            location=FIXED_LOCATION,
        )
        assert student_only_result["success"] is False
        assert student_only_result["authorization_required"] is True

        late_result = late_attendance.sign(
            Course("c-1", "课程", "class-1", "班级"),
            late_activity,
            location=FIXED_LOCATION,
        )
        assert late_result["success"] is True
        assert late_result["attendance_state"] == "已签到"
        assert late_attendance.is_signed(late_activity) is True
        assert late_activity["personStatus"] == 1
        assert late_activity["status"] == 1
        assert late_client.post_body is not None
        print("self-test: PASS")
        return 0

    config, client, course_service, activity_service = _make_services(Path(args.config), args.token)
    courses = course_service.list_courses()
    if args.command == "list-courses":
        if args.json:
            _print_json([asdict(course) for course in courses])
        else:
            for course in courses:
                print(f"{course.course_id}\t{course.name}\t{course.class_name}")
        return 0

    selected_courses = courses
    if getattr(args, "course_id", None):
        selected_courses = [course for course in courses if course.course_id == args.course_id]
        if not selected_courses:
            print(f"未找到课程: {args.course_id}")
            return 2

    if args.command == "selection-evidence":
        if not args.activity_id:
            print("selection-evidence 需要 --activity-id")
            return 2
        course = selected_courses[0]
        activities = activity_service.list_activities(course)
        activity = AttendanceService._find_activity(activities, args.activity_id)
        if activity is None:
            print(f"课程 {course.course_id} 中未找到活动: {args.activity_id}")
            return 2
        evidence = build_selection_evidence(
            course,
            activity,
            classroom_id=args.classroom_id,
            activity_service=activity_service,
        )
        _print_json(evidence)
        if args.output:
            Path(args.output).write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            print(f"选人证据已保存: {args.output}")
        return 0

    if args.command == "list-selection":
        items: list[SelectionActivity] = []
        for course in selected_courses:
            items.extend(activity_service.list_selection(course, active_only=args.active_only))
        _print_json([item.to_dict() for item in items])
        return 0

    if args.command == "diagnose":
        diagnostics: list[dict[str, Any]] = []
        for course in selected_courses:
            activities = activity_service.list_activities(course)
            diagnostics.append(_diagnostic(course, activities, base_url=config.lms_base_url))
        value: Any = diagnostics[0] if args.course_id and len(diagnostics) == 1 else diagnostics
        _print_json(value)
        if args.output:
            Path(args.output).write_text(
                json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            print(f"诊断已保存: {args.output}")
        return 0

    if args.command == "signin":
        if not args.course_id:
            print("signin 需要 --course-id")
            return 2
        course = selected_courses[0]
        activities = activity_service.list_activities(course)
        activity = AttendanceService._find_activity(activities, args.activity_id)
        if activity is None:
            print(f"课程 {course.course_id} 中未找到点名活动: {args.activity_id}")
            return 2
        teacher_token = args.teacher_token or os.getenv("ULEARNING_TEACHER_AUTHORIZATION", "")
        teacher_user_id = args.teacher_user_id or _as_int(
            os.getenv("ULEARNING_TEACHER_USER_ID", ""), 0
        ) or 0
        service = AttendanceService(
            client,
            activity_service,
            application_base_url=config.application_base_url,
            user_id=config.user_id,
            teacher_client=(
                HttpClient(
                    token=teacher_token,
                    timeout=config.timeout,
                    retries=config.retries,
                    backoff=config.backoff,
                    session=requests.Session(),
                )
                if teacher_token
                else None
            ),
            teacher_user_id=teacher_user_id,
            verify_delay=config.verify_delay,
            history_file=_resolve_config_path(Path(args.config), config.history_file),
            allow_ended=not getattr(args, "active_only", False),
        )
        sign_options = {
            "classroom_id": args.classroom_id,
            "location": args.location or config.default_location,
            "address": args.address or config.default_address,
            "coord_system": args.coord_system or config.default_coord_system,
            "enter_way": args.enter_way,
            "attendance_code": args.attendance_code,
        }
        preview = service.sign(course, activity, **sign_options, dry_run=True)
        _print_json(preview)
        if args.dry_run:
            return 0
        if "request" not in preview:
            return 0 if preview.get("success") else 1
        if not args.confirm:
            try:
                answer = input("确认发送这一次签到请求？输入 yes 继续：").strip().lower()
            except EOFError:
                answer = ""
            if answer not in {"y", "yes"}:
                print("已取消，未发送请求")
                return 0
        result = service.sign(
            course,
            activity,
            **sign_options,
            dry_run=False,
        )
        _print_json(result)
        return 0 if result.get("success") or args.dry_run else 1

    view_attendance = AttendanceService(
        client,
        activity_service,
        application_base_url=config.application_base_url,
        user_id=config.user_id,
        verify_delay=config.verify_delay,
        history_file=_resolve_config_path(Path(args.config), config.history_file),
    )
    activities: list[dict[str, Any]] = []
    for course in selected_courses:
        course_activities = activity_service.list_activities(course)
        if getattr(args, "active_only", False):
            course_activities = [activity for activity in course_activities if activity_is_active(activity)]
        activities.extend(course_activities)
    _print_json(_activity_rows(activities, signed_checker=view_attendance.is_signed))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="优学院课程、活动与单次签到工具")
    parser.add_argument("--config", default=str(Path(__file__).with_name("ulearning-cmd.json")))
    parser.add_argument(
        "--teacher-token",
        help="server-issued teacher/admin AUTHORIZATION for ended-record writes",
    )
    parser.add_argument(
        "--teacher-user-id",
        type=int,
        default=0,
        help="optional teacher/admin userID for diagnostics (not used to impersonate)",
    )
    parser.add_argument("--token", help="已有 AUTHORIZATION，不会写入文件")
    parser.add_argument(
        "--verbose", action="store_true", help="输出请求重试、接口 URL 等脱敏诊断信息"
    )
    parser.add_argument(
        "--log-file",
        default=str(Path(__file__).with_name("ulearning.log")),
        help="日志文件路径，不会写入 token 或密码",
    )
    commands = parser.add_subparsers(dest="command")

    courses = commands.add_parser("list-courses", help="查询课程")
    courses.add_argument("--json", action="store_true")

    activities = commands.add_parser("list-activities", help="查询指定课程的全部活动")
    activities.add_argument("--course-id")
    activities.add_argument("--active-only", action="store_true")
    activities.add_argument("--json", action="store_true")

    selection = commands.add_parser("list-selection", help="查询选人活动")
    selection.add_argument("--course-id")
    selection.add_argument("--active-only", action="store_true")
    selection.add_argument("--json", action="store_true")

    evidence = commands.add_parser("selection-evidence", help="读取选人活动的扩展只读数据")
    evidence.add_argument("--course-id", required=True)
    evidence.add_argument("--activity-id", required=True, help="选人活动 relationId")
    evidence.add_argument("--classroom-id", default="", help="可选课堂 ID；省略时自动解析")
    evidence.add_argument("--output", help="可选 JSON 输出路径")

    check = commands.add_parser("check", help="查询课程活动状态")
    check.add_argument("--course-id")
    check.add_argument("--active-only", action="store_true")
    check.add_argument("--json", action="store_true")

    diagnose = commands.add_parser("diagnose", help="输出脱敏的接口与字段诊断")
    diagnose.add_argument("--course-id")
    diagnose.add_argument("--output", help="可选诊断 JSON 输出路径")

    signin = commands.add_parser(
        "signin", help="对一个指定的点名活动发送签到请求（包含已结束活动）"
    )
    signin.add_argument("--course-id", required=True)
    signin.add_argument("--activity-id", required=True, help="点名活动 relationId")
    signin.add_argument("--classroom-id", default="", help="课堂 ID；省略时自动解析")
    signin.add_argument("--location", default="", help="签到位置，经度,纬度；默认使用配置 default_location")
    signin.add_argument("--address", default="", help="真实位置文字")
    signin.add_argument(
        "--coord-system",
        choices=COORD_SYSTEMS,
        default="",
        help="输入坐标系：gcj02(默认)、wgs84(GPS)、bd09(百度)",
    )
    signin.add_argument("--attendance-code", default="", help="教师提供的签到码")
    signin.add_argument("--enter-way", type=int, default=1, help="进入方式，默认 1")
    signin.add_argument("--dry-run", action="store_true", help="只显示待发送请求，不写入服务器")
    signin.add_argument("--confirm", action="store_true", help="跳过交互确认，直接发送一次请求")
    signin.add_argument(
        "--active-only",
        action="store_true",
        help="仅允许进行中的活动；默认也会尝试已结束活动的补签请求",
    )
    commands.add_parser("self-test", help="运行本地状态解析测试")
    return parser


def launch_gui(config_path: Path) -> int:
    try:
        import tkinter as tk
        from tkinter import messagebox, ttk
    except ImportError as exc:
        print("当前 Python 没有安装 Tkinter，无法启动图形界面。", exc)
        return 1

    class App(tk.Tk):
        def __init__(self) -> None:
            super().__init__()
            self.title("优学院活动与签到工具")
            self.geometry("1080x680")
            self.minsize(820, 520)
            self.courses: list[Course] = []
            self.course_by_label: dict[str, Course] = {}
            self.course_service: CourseService | None = None
            self.activity_service: ActivityService | None = None
            self.attendance_service: AttendanceService | None = None
            self.activity_rows: list[dict[str, Any]] = []
            # Show ended activities by default so they remain selectable for
            # a server-supported late sign request.
            self.active_only = tk.BooleanVar(value=False)
            self.course_var = tk.StringVar()
            self.status_var = tk.StringVar(value="准备就绪")
            self.location_var = tk.StringVar()
            self.address_var = tk.StringVar()
            self.coord_system_var = tk.StringVar(value="gcj02")
            self.attendance_code_var = tk.StringVar()
            self.pending_sign: tuple[Course, dict[str, Any], dict[str, str]] | None = None
            self.selection_snapshot: dict[str, Any] | None = None
            self.config_path = config_path
            self._build()

        def _build(self) -> None:
            toolbar = ttk.Frame(self, padding=10)
            toolbar.pack(fill=tk.X)
            ttk.Label(toolbar, text="配置文件").pack(side=tk.LEFT)
            self.config_entry = ttk.Entry(toolbar, width=52)
            self.config_entry.insert(0, str(self.config_path))
            self.config_entry.pack(side=tk.LEFT, padx=(6, 12))
            self.load_button = ttk.Button(toolbar, text="加载课程", command=self.load_courses)
            self.load_button.pack(side=tk.LEFT)
            ttk.Button(toolbar, text="打开官方页面", command=lambda: webbrowser.open(OFFICIAL_PAGE)).pack(
                side=tk.LEFT, padx=6
            )
            ttk.Label(toolbar, textvariable=self.status_var).pack(side=tk.RIGHT)

            controls = ttk.Frame(self, padding=(10, 0, 10, 8))
            controls.pack(fill=tk.X)
            ttk.Label(controls, text="课程").pack(side=tk.LEFT)
            self.course_combo = ttk.Combobox(
                controls, textvariable=self.course_var, state="disabled", width=55
            )
            self.course_combo.pack(side=tk.LEFT, padx=6)
            self.selection_button = ttk.Button(
                controls, text="查询选人", command=self.query_selection, state="disabled"
            )
            self.selection_button.pack(side=tk.LEFT, padx=3)
            self.activities_button = ttk.Button(
                controls, text="查询全部活动", command=self.query_activities, state="disabled"
            )
            self.activities_button.pack(side=tk.LEFT, padx=3)
            self.signin_button = ttk.Button(
                controls, text="对选中点名签到", command=self.sign_selected, state="disabled"
            )
            self.signin_button.pack(side=tk.LEFT, padx=3)
            self.diagnostic_button = ttk.Button(
                controls, text="诊断", command=self.query_diagnostic, state="disabled"
            )
            self.diagnostic_button.pack(side=tk.LEFT, padx=3)
            self.selection_evidence_button = ttk.Button(
                controls, text="选人详情", command=self.query_selection_evidence, state="disabled"
            )
            self.selection_evidence_button.pack(side=tk.LEFT, padx=3)
            self.selection_snapshot_button = ttk.Button(
                controls, text="记录选人快照", command=self.record_selection_snapshot, state="disabled"
            )
            self.selection_snapshot_button.pack(side=tk.LEFT, padx=3)
            self.selection_compare_button = ttk.Button(
                controls, text="对比快照", command=self.compare_selection_snapshot, state="disabled"
            )
            self.selection_compare_button.pack(side=tk.LEFT, padx=3)
            ttk.Checkbutton(
                controls,
                text="只看进行中（取消后含已结束）",
                variable=self.active_only,
            ).pack(
                side=tk.LEFT, padx=8
            )
            ttk.Button(controls, text="清空", command=self.clear_output).pack(side=tk.RIGHT)

            fields = ttk.Frame(self, padding=(10, 0, 10, 8))
            fields.pack(fill=tk.X)
            ttk.Label(fields, text="位置(经度,纬度)").pack(side=tk.LEFT)
            self.location_var.set(FIXED_LOCATION)
            self.location_entry = ttk.Entry(fields, textvariable=self.location_var, width=24)
            self.location_entry.pack(side=tk.LEFT, padx=(6, 10))
            ttk.Label(fields, text="坐标系").pack(side=tk.LEFT)
            self.coord_system_combo = ttk.Combobox(
                fields,
                textvariable=self.coord_system_var,
                values=COORD_SYSTEMS,
                width=7,
                state="readonly",
            )
            self.coord_system_combo.pack(side=tk.LEFT, padx=(6, 10))
            ttk.Label(fields, text="地址").pack(side=tk.LEFT)
            self.address_entry = ttk.Entry(fields, textvariable=self.address_var, width=24)
            self.address_entry.pack(side=tk.LEFT, padx=(6, 10))
            ttk.Label(fields, text="签到码").pack(side=tk.LEFT)
            self.attendance_code_entry = ttk.Entry(
                fields, textvariable=self.attendance_code_var, width=12
            )
            self.attendance_code_entry.pack(side=tk.LEFT, padx=(6, 10))
            self.auto_signin_button = ttk.Button(
                fields,
                text="一键签到当前课程",
                command=self.auto_sign_current_course,
                state="disabled",
            )
            self.auto_signin_button.pack(side=tk.LEFT, padx=(10, 0))

            table_frame = ttk.Frame(self, padding=(10, 0, 10, 8))
            table_frame.pack(fill=tk.BOTH, expand=True)
            columns = (
                "course",
                "activity_id",
                "type",
                "title",
                "status",
                "time_display",
                "start_time",
                "end_time",
                "score",
                "attendance",
                "selection",
            )
            self.activity_tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse")
            headings = {
                "course": "课程",
                "activity_id": "活动 ID",
                "type": "类型",
                "title": "标题",
                "status": "状态",
                "time_display": "服务器时间状态",
                "start_time": "开始时间",
                "end_time": "结束时间",
                "score": "得分",
                "attendance": "签到状态",
                "selection": "选人状态",
            }
            widths = {
                "course": 160,
                "activity_id": 80,
                "type": 80,
                "title": 360,
                "status": 80,
                "time_display": 110,
                "start_time": 140,
                "end_time": 140,
                "score": 60,
                "attendance": 90,
                "selection": 90,
            }
            for column in columns:
                self.activity_tree.heading(column, text=headings[column])
                self.activity_tree.column(column, width=widths[column], anchor=tk.W)
            tree_scroll = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.activity_tree.yview)
            tree_horizontal = ttk.Scrollbar(
                table_frame, orient=tk.HORIZONTAL, command=self.activity_tree.xview
            )
            self.activity_tree.configure(
                yscrollcommand=tree_scroll.set, xscrollcommand=tree_horizontal.set
            )
            self.activity_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            tree_scroll.pack(side=tk.RIGHT, fill=tk.Y)
            tree_horizontal.pack(side=tk.BOTTOM, fill=tk.X)
            self.activity_tree.bind("<<TreeviewSelect>>", lambda _event: self._update_signin_state())

            output_frame = ttk.Frame(self, padding=(10, 0, 10, 10))
            output_frame.pack(fill=tk.X)
            ttk.Label(output_frame, text="请求日志").pack(anchor=tk.W)
            self.output = tk.Text(output_frame, height=7, wrap=tk.NONE, state=tk.DISABLED, font=("Consolas", 9))
            self.output.pack(fill=tk.X)
            self._write(
                "活动列表来自官方学生端接口；选人详情/快照仅做只读采集；定位签到使用上方位置输入。\n"
            )

        def _write(self, text: str) -> None:
            self.output.configure(state=tk.NORMAL)
            self.output.insert(tk.END, text)
            self.output.see(tk.END)
            self.output.configure(state=tk.DISABLED)

        def clear_output(self) -> None:
            self.output.configure(state=tk.NORMAL)
            self.output.delete("1.0", tk.END)
            self.output.configure(state=tk.DISABLED)
            self._clear_activity_rows()

        def _set_busy(self, busy: bool) -> None:
            state = tk.DISABLED if busy else tk.NORMAL
            self.load_button.configure(state=state)
            self.selection_button.configure(state=state if self.courses else tk.DISABLED)
            self.activities_button.configure(state=state if self.courses else tk.DISABLED)
            self.diagnostic_button.configure(state=state if self.courses else tk.DISABLED)
            self.selection_evidence_button.configure(state=state if self.courses else tk.DISABLED)
            self.selection_snapshot_button.configure(state=state if self.courses else tk.DISABLED)
            self.selection_compare_button.configure(state=state if self.courses else tk.DISABLED)
            self.auto_signin_button.configure(state=state if self.courses else tk.DISABLED)
            if busy:
                self.signin_button.configure(state=tk.DISABLED)
            else:
                self._update_signin_state()
            self.course_combo.configure(state="disabled" if busy else "readonly")
            self.status_var.set("请求中..." if busy else "完成")

        def _services(self) -> tuple[CourseService, ActivityService]:
            config, client, course_service, activity_service = _make_services(
                Path(self.config_entry.get().strip())
            )
            current_location = self.location_var.get().strip()
            if not current_location or current_location == FIXED_LOCATION:
                self.location_var.set(config.default_location)
            if not self.address_var.get().strip() and config.default_address:
                self.address_var.set(config.default_address)
            if self.coord_system_var.get().strip() == "gcj02":
                self.coord_system_var.set(config.default_coord_system)
            self.attendance_service = AttendanceService(
                client,
                activity_service,
                application_base_url=config.application_base_url,
                user_id=config.user_id,
                verify_delay=config.verify_delay,
                history_file=_resolve_config_path(
                    Path(self.config_entry.get().strip()), config.history_file
                ),
            )
            return course_service, activity_service

        def _run_async(self, operation: Callable[[], Any], callback: Callable[[Any], None]) -> None:
            self._set_busy(True)

            def worker() -> None:
                try:
                    result = operation()
                except Exception as exc:
                    self.after(0, lambda error=exc: self._failed(error))
                    return
                self.after(0, lambda value=result: self._done(callback, value))

            threading.Thread(target=worker, daemon=True).start()

        def _done(self, callback: Callable[[Any], None], value: Any) -> None:
            self._set_busy(False)
            callback(value)

        def _failed(self, exc: Exception) -> None:
            self._set_busy(False)
            self.status_var.set("失败")
            self._write(f"错误：{exc}\n")
            messagebox.showerror("请求失败", str(exc))

        def load_courses(self) -> None:
            def operation() -> tuple[CourseService, ActivityService, list[Course]]:
                course_service, activity_service = self._services()
                return course_service, activity_service, course_service.list_courses()

            self._run_async(operation, self._courses_loaded)

        def _courses_loaded(self, result: tuple[CourseService, ActivityService, list[Course]]) -> None:
            self.course_service, self.activity_service, self.courses = result
            self.course_by_label = {
                f"{course.course_id} | {course.name}": course for course in self.courses
            }
            labels = list(self.course_by_label)
            self.course_combo["values"] = labels
            self.course_combo.configure(state="readonly" if labels else "disabled")
            self.selection_button.configure(state=tk.NORMAL if labels else tk.DISABLED)
            self.activities_button.configure(state=tk.NORMAL if labels else tk.DISABLED)
            self.diagnostic_button.configure(state=tk.NORMAL if labels else tk.DISABLED)
            self.selection_evidence_button.configure(state=tk.NORMAL if labels else tk.DISABLED)
            self.selection_snapshot_button.configure(state=tk.NORMAL if labels else tk.DISABLED)
            self.selection_compare_button.configure(
                state=tk.NORMAL if (labels and self.selection_snapshot is not None) else tk.DISABLED
            )
            self.auto_signin_button.configure(state=tk.NORMAL if labels else tk.DISABLED)
            if labels:
                self.course_combo.current(0)
            self._write(f"已加载 {len(labels)} 门课程。\n")
            self._clear_activity_rows()

        def _current_course(self) -> Course | None:
            return self.course_by_label.get(self.course_var.get())

        def query_selection(self) -> None:
            course = self._current_course()
            if course is None or self.activity_service is None:
                messagebox.showinfo("提示", "请先加载课程并选择课程")
                return
            self._run_async(lambda: self.activity_service.list_activities(course), self._selection_loaded)

        def _selection_loaded(self, activities: list[dict[str, Any]]) -> None:
            filtered = [
                activity for activity in activities
                if _as_int(activity.get("relationType")) == 4
                and (not self.active_only.get() or activity_is_active(activity))
            ]
            self._show_activities(filtered)
            self._write(f"查询到 {len(filtered)} 个选人活动。\n")

        def query_activities(self) -> None:
            course = self._current_course()
            if course is None or self.activity_service is None:
                messagebox.showinfo("提示", "请先加载课程并选择课程")
                return
            self._run_async(
                lambda: self.activity_service.list_activities(course),
                self._activities_loaded,
            )

        def query_diagnostic(self) -> None:
            course = self._current_course()
            if course is None or self.activity_service is None:
                messagebox.showinfo("提示", "请先加载课程并选择课程")
                return
            service = self.activity_service
            self._run_async(
                lambda: _diagnostic(
                    course,
                    service.list_activities(course),
                    base_url=service.lms_base_url,
                ),
                lambda value: self._write(json.dumps(value, ensure_ascii=False, indent=2) + "\n"),
            )

        def _selected_selection_activity(self) -> tuple[Course | None, dict[str, Any] | None]:
            course, activity = self._selected_activity()
            if activity is None or _as_int(activity.get("relationType")) != 4:
                return None, None
            return course, activity

        def query_selection_evidence(self) -> None:
            course, activity = self._selected_selection_activity()
            if course is None or activity is None or self.activity_service is None:
                messagebox.showinfo("提示", "请先查询选人，并在列表中选择一个 relationType=4 的活动")
                return
            service = self.activity_service
            activity_id = str(activity.get("relationId", activity.get("id", "")))
            self._run_async(
                lambda: build_selection_evidence(
                    course,
                    activity,
                    activity_service=service,
                ),
                lambda value: self._write(
                    "选人扩展详情：\n" + json.dumps(value, ensure_ascii=False, indent=2) + "\n"
                ),
            )

        def _selection_snapshot_path(self) -> Path:
            configured = self.config_entry.get().strip()
            return Path(configured).resolve().parent / "ulearning_selection_snapshot.json"

        def record_selection_snapshot(self) -> None:
            course, activity = self._selected_selection_activity()
            if course is None or activity is None or self.activity_service is None:
                messagebox.showinfo("提示", "请先选择一个选人活动")
                return
            service = self.activity_service

            def operation() -> dict[str, Any]:
                evidence = build_selection_evidence(
                    course,
                    activity,
                    activity_service=service,
                )
                evidence["snapshot_kind"] = "baseline"
                return evidence

            self._run_async(operation, self._selection_snapshot_recorded)

        def _selection_snapshot_recorded(self, evidence: Mapping[str, Any]) -> None:
            snapshot = dict(evidence)
            self.selection_snapshot = snapshot
            path = self._selection_snapshot_path()
            try:
                path.write_text(
                    json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                self._write(
                    f"已记录选人基准快照：活动 {snapshot.get('activity_id')}；保存到 {path}\n"
                )
            except OSError as exc:
                self._write(f"快照已保存在内存中，但保存文件失败：{exc}\n")
            self.selection_compare_button.configure(state=tk.NORMAL)

        def _load_selection_snapshot(self) -> dict[str, Any] | None:
            if self.selection_snapshot is not None:
                return dict(self.selection_snapshot)
            path = self._selection_snapshot_path()
            if not path.exists():
                return None
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                return None
            return dict(value) if isinstance(value, Mapping) else None

        def compare_selection_snapshot(self) -> None:
            course, activity = self._selected_selection_activity()
            if course is None or activity is None or self.activity_service is None:
                messagebox.showinfo("提示", "请先选择一个选人活动")
                return
            before = self._load_selection_snapshot()
            if before is None:
                messagebox.showinfo("提示", "还没有基准快照，请先点击“记录选人快照”")
                return
            activity_id = str(activity.get("relationId", activity.get("id", "")))
            if str(before.get("activity_id", "")) != activity_id:
                messagebox.showwarning(
                    "快照不匹配",
                    f"基准活动是 {before.get('activity_id')}，当前活动是 {activity_id}。请先重新记录基准快照。",
                )
                return
            service = self.activity_service

            def operation() -> dict[str, Any]:
                after = build_selection_evidence(
                    course,
                    activity,
                    activity_service=service,
                )
                return {
                    "before_captured_at": before.get("captured_at"),
                    "after_captured_at": after.get("captured_at"),
                    "activity_id": activity_id,
                    "student_activity_changes": _diff_json(
                        before.get("student_activity", {}),
                        after.get("student_activity", {}),
                    ),
                    "classroom_match_changes": _diff_json(
                        (before.get("classroom_matches") or [{}])[0],
                        (after.get("classroom_matches") or [{}])[0],
                    ),
                    "student_vs_classroom_after": after.get("student_vs_classroom_diff", []),
                    "after_evidence": after,
                }

            self._run_async(
                operation,
                lambda result: self._write(
                    "选人前后差异：\n" + json.dumps(result, ensure_ascii=False, indent=2) + "\n"
                ),
            )

        def _activities_loaded(self, activities: list[dict[str, Any]]) -> None:
            filtered = [
                activity for activity in activities
                if not self.active_only.get() or activity_is_active(activity)
            ]
            self._show_activities(filtered)
            self._write(f"查询到 {len(filtered)} 个活动。\n")

        def _clear_activity_rows(self) -> None:
            self.activity_rows = []
            for item in self.activity_tree.get_children():
                self.activity_tree.delete(item)
            self._update_signin_state()

        def _show_activities(self, activities: list[dict[str, Any]]) -> None:
            self._clear_activity_rows()
            self.activity_rows = activities
            for index, activity in enumerate(activities):
                relation_type = _as_int(activity.get("relationType"), -1) or -1
                selection = ""
                if relation_type in (3, 4):
                    selection = selection_state(activity)[0]
                attendance = ""
                if relation_type == 1:
                    attendance = "已签到" if self._attendance_is_signed(activity) else "未签到"
                course_name = str(activity.get("_courseName", ""))
                self.activity_tree.insert(
                    "",
                    tk.END,
                    iid=str(index),
                    values=(
                        course_name,
                        activity.get("relationId", ""),
                        RELATION_TYPE.get(relation_type, f"未知({relation_type})"),
                        str(activity.get("title", "")).replace("\n", " "),
                        activity_status(activity),
                        activity.get("timeDisplay", ""),
                        _activity_times(activity)[0] or "",
                        _activity_times(activity)[1] or "",
                        activity.get("score", ""),
                        attendance,
                        selection,
                    ),
                )
            self._update_signin_state()

        def _attendance_is_signed(self, activity: Mapping[str, Any]) -> bool:
            service = self.attendance_service
            return service.is_signed(activity) if service is not None else attendance_signed(activity)

        def _selected_activity(self) -> tuple[Course | None, dict[str, Any] | None]:
            selected = self.activity_tree.selection()
            if not selected:
                return None, None
            try:
                activity = self.activity_rows[int(selected[0])]
            except (IndexError, ValueError):
                return None, None
            course_id = str(activity.get("_courseId", ""))
            return next((course for course in self.courses if course.course_id == course_id), None), activity

        def _update_signin_state(self) -> None:
            if not hasattr(self, "signin_button"):
                return
            course, activity = self._selected_activity()
            enabled = bool(
                course is not None
                and activity is not None
                and _as_int(activity.get("relationType")) == 1
                and not self._attendance_is_signed(activity)
                and AttendanceService.can_sign(
                    activity,
                    allow_ended=(
                        self.attendance_service.allow_ended
                        if self.attendance_service is not None
                        else True
                    ),
                )[0]
            )
            self.signin_button.configure(state=tk.NORMAL if enabled else tk.DISABLED)

        def sign_selected(self) -> None:
            course, activity = self._selected_activity()
            if course is None or activity is None or self.attendance_service is None:
                messagebox.showinfo("提示", "请先查询并选择一个可签到的点名活动")
                return
            if self._attendance_is_signed(activity):
                messagebox.showinfo("签到状态", "已签到")
                return
            can_sign, reason = AttendanceService.can_sign(
                activity, allow_ended=self.attendance_service.allow_ended
            )
            if not can_sign:
                messagebox.showinfo("签到状态", reason)
                return
            options = self._signin_options()
            self.pending_sign = (course, activity, options)
            self._write(
                f"准备签到：{activity.get('title', '')}，位置 {options['location']} ({options['coord_system']})\n"
            )
            self._run_async(
                lambda: self.attendance_service.sign(course, activity, **options, dry_run=False),
                self._signin_finished,
            )

        def _signin_options(self) -> dict[str, str]:
            return {
                "location": self.location_var.get().strip(),
                "address": self.address_var.get().strip(),
                "coord_system": self.coord_system_var.get().strip() or "gcj02",
                "attendance_code": self.attendance_code_var.get().strip(),
            }

        @staticmethod
        def _has_signin_options(options: Mapping[str, str]) -> bool:
            return any(str(value).strip() for value in options.values())

        def auto_sign_current_course(self) -> None:
            course = self._current_course()
            activity_service = self.activity_service
            attendance_service = self.attendance_service
            if course is None or activity_service is None or attendance_service is None:
                messagebox.showinfo("提示", "请先加载课程并选择课程")
                return
            options = self._signin_options()

            def operation() -> dict[str, Any]:
                activities = activity_service.list_activities(course)
                candidates = signable_attendance_activities(
                    activities,
                    allow_ended=attendance_service.allow_ended,
                    signed_checker=attendance_service.is_signed,
                )
                if len(candidates) != 1:
                    return {
                        "kind": "none" if not candidates else "multiple",
                        "activities": candidates,
                    }
                result = attendance_service.sign(
                    course,
                    candidates[0],
                    **options,
                    dry_run=False,
                )
                return {"kind": "signed", "activities": candidates, "result": result}

            self._run_async(
                operation,
                self._auto_sign_finished,
            )

        def _auto_sign_finished(self, outcome: Mapping[str, Any]) -> None:
            activities = list(outcome.get("activities", []))
            self._show_activities(activities)
            kind = outcome.get("kind")
            if kind == "none":
                self._write("当前课程没有进行中且未签到的点名活动。\n")
                messagebox.showinfo("一键签到", "当前课程没有可签到的点名活动。")
                return
            if kind == "multiple":
                self._write(f"找到 {len(activities)} 个可签到点名，请在列表中选择目标。\n")
                messagebox.showinfo(
                    "一键签到",
                    f"找到 {len(activities)} 个可签到点名。为避免签错，已列出活动，请选择后使用“对选中点名签到”。",
                )
                return
            result = outcome.get("result")
            if not isinstance(result, Mapping):
                raise SchemaError("一键签到结果结构无效")
            self._signin_finished(result)

        def _signin_preview(self, preview: Mapping[str, Any]) -> None:
            # 保留兼容接口；当前“对选中点名签到”已改为直接发送正式请求。
            self._write(json.dumps(preview, ensure_ascii=False, indent=2) + "\n")

        def _signin_finished(self, result: Mapping[str, Any]) -> None:
            self._write(json.dumps(dict(result), ensure_ascii=False, indent=2) + "\n")
            messagebox.showinfo("签到结果", str(result.get("message", "请求完成")))
            if self._current_course() is not None and self.activity_service is not None:
                self.query_activities()

    try:
        App().mainloop()
    except Exception as exc:
        print(f"图形界面启动失败：{exc}")
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    _configure_console_encoding()
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        log_path = Path(args.log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
    except OSError as exc:
        print(f"日志文件不可用，将仅输出到控制台: {exc}")
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        handlers=handlers,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.command is None:
        return launch_gui(Path(args.config))
    try:
        return run_cli(args)
    except (ApiError, ConfigError, RuntimeError) as exc:
        LOGGER.exception("command failed")
        print(f"错误：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
