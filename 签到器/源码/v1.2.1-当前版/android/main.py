"""优学院 DGUT 签到助手 —— 移动端入口（Kivy 重制版 UI）。

打包目标：Buildozer / python-for-android（GitHub Actions 在线构建）。
桌面调试：pip install kivy requests && python main.py

架构说明：
    main.py(本文件, UI 层)  ->  test3.py(核心服务层, 原样复用, 未改动)

UI 重制要点：
    - 现代卡片式布局、圆角按钮/彩色徽章/底部导航，替代原版平铺控件
    - 内置 NotoSansSC 可变字重字体，注册为默认字体，全组件中文无乱码
    - 功能项：登录(记住登录)/切换用户/课程加载/
      查询全部活动/查询选人/只看进行中/单条签到/对选中签到/一键签到/
      位置-坐标系-地址-签到码/诊断/选人详情/选人快照记录与对比/
      刷新课程/打开官方页面/日志查看与清空/状态条与结果弹窗
"""

from __future__ import annotations

# build: 626169736875797565-2026
__build_ref__ = "YmFpc2h1eXll"

import json
import threading
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

import test3
from test3 import (
    OFFICIAL_PAGE,
    RELATION_TYPE,
    ActivityService,
    AppConfig,
    AttendanceService,
    AuthenticationError,
    Course,
    CourseService,
    HttpClient,
    LoginService,
    _as_int,
    _diagnostic,
    _diff_json,
    activity_is_active,
    activity_status,
    attendance_signed,
    build_selection_evidence,
    load_config,
    selection_state,
    signable_attendance_activities,
)

from kivy.app import App
from kivy.clock import Clock
from kivy.core.text import LabelBase
from kivy.graphics import Color, Line, Rectangle, RoundedRectangle
from kivy.metrics import dp, sp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.checkbox import CheckBox
from kivy.uix.floatlayout import FloatLayout
from kivy.uix.gridlayout import GridLayout
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.screenmanager import Screen, SlideTransition, ScreenManager
from kivy.uix.scrollview import ScrollView
from kivy.uix.spinner import Spinner, SpinnerOption
from kivy.uix.textinput import TextInput


# ---------------------------------------------------------------------------
# 字体：注册 NotoSansSC，并覆盖 Kivy 默认 Roboto，全局生效（中文无乱码）
#
# 说明：使用由内置可变字体（NotoSansSC-VF.ttf）实例化出的静态字重文件。
# 部分 SDL2_ttf 版本在渲染可变字重 TTF 时会崩溃，静态字体可彻底规避。
# ---------------------------------------------------------------------------

FONT_REGULAR = "NotoSansSC-Regular.ttf"
FONT_BOLD = "NotoSansSC-Bold.ttf"
FONT_NAME = "NotoSansSC"


# Android 系统自带 CJK 字体，作为最后兜底（保证中文绝不乱码/方块）
_SYSTEM_CJK = (
    "/system/fonts/NotoSansCJK-Regular.ttc",
    "/system/fonts/NotoSansSC-Regular.otf",
    "/system/fonts/DroidSansFallback.ttf",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
)


def _find_font(name: str) -> str:
    here = Path(__file__).resolve().parent
    for cand in (
        here / "fonts" / name,
        here / name,
        Path.cwd() / "fonts" / name,
    ):
        try:
            if cand.exists():
                return str(cand)
        except OSError:
            pass
    for cand in _SYSTEM_CJK:
        try:
            if Path(cand).exists():
                return cand
        except OSError:
            pass
    return ""


FONT_PATH = _find_font(FONT_REGULAR)
FONT_BOLD_PATH = _find_font(FONT_BOLD)
if FONT_PATH and FONT_BOLD_PATH:
    LabelBase.register(FONT_NAME, fn_regular=FONT_PATH, fn_bold=FONT_BOLD_PATH)
    LabelBase.register("Roboto", fn_regular=FONT_PATH, fn_bold=FONT_BOLD_PATH)
elif FONT_PATH:
    LabelBase.register(FONT_NAME, fn_regular=FONT_PATH)
    LabelBase.register("Roboto", fn_regular=FONT_PATH)


# ---------------------------------------------------------------------------
# 配色与基础组件
# ---------------------------------------------------------------------------

C = {
    "bg": (0.953, 0.957, 0.973, 1),        # 页面背景 #F3F4F8
    "card": (1, 1, 1, 1),                   # 卡片
    "primary": (0.231, 0.357, 0.859, 1),    # 主色 #3B5BDB
    "primary_dark": (0.176, 0.278, 0.690, 1),
    "primary_bg": (0.886, 0.910, 0.984, 1), # 主色浅底
    "teal": (0.047, 0.651, 0.471, 1),       # 一键签到 #0CA678
    "teal_bg": (0.874, 0.965, 0.929, 1),
    "danger": (0.878, 0.196, 0.196, 1),     # #E03131
    "danger_bg": (0.988, 0.898, 0.898, 1),
    "purple": (0.439, 0.282, 0.910, 1),     # 选人 #7048E8
    "purple_bg": (0.910, 0.886, 0.984, 1),
    "warn": (0.937, 0.600, 0.078, 1),
    "warn_bg": (0.996, 0.949, 0.859, 1),
    "text": (0.129, 0.161, 0.216, 1),       # 主文字 #212637
    "text_sub": (0.435, 0.470, 0.525, 1),   # 次级文字
    "line": (0.878, 0.898, 0.929, 1),       # 边框线
    "chip_gray": (0.914, 0.922, 0.937, 1),
    "white": (1, 1, 1, 1),
    "disabled": (0.745, 0.773, 0.824, 1),
    "dark": (0.098, 0.113, 0.157, 1),       # 日志区深底
    "dark_text": (0.847, 0.886, 0.937, 1),
}

STATUS_COLORS = {
    "success": (C["teal_bg"], C["teal"]),
    "error": (C["danger_bg"], C["danger"]),
    "info": (C["primary_bg"], C["primary"]),
}


def L(
    text: str = "",
    size: float = 14,
    color: tuple = C["text"],
    bold: bool = False,
    **kw: Any,
) -> Label:
    """统一 Label 工厂：中文默认字体 + 自动 text_size（halign/valign 生效）。

    默认 valign="middle"：Kivy 的默认值是 "bottom"，在固定高度的行里会让
    文字贴底，和相邻控件挤在一起看起来像"重叠/压字"。统一居中可根治这一类
    观感问题。
    """
    kw.setdefault("font_name", FONT_NAME)
    kw.setdefault("font_size", sp(size))
    kw.setdefault("color", color)
    kw.setdefault("bold", bold)
    kw.setdefault("valign", "middle")
    label = Label(text=str(text), **kw)
    label.bind(size=lambda w, _s: setattr(w, "text_size", _s))
    return label


def short_label(text: str, size: float = 14, color: tuple = C["text"], **kw: Any) -> Label:
    """单行 Label：超长时右端省略，绝不折行、绝不超出自身宽度。

    用于标题、状态条这类"行高固定"的位置——文本一旦折行就会溢出到相邻
    行，视觉上表现为文字互相叠压。
    """
    kw.setdefault("halign", "left")
    kw.setdefault("shorten", True)
    kw.setdefault("shorten_from", "right")
    kw.setdefault("valign", "middle")
    return L(text, size, color, **kw)


def _brief(text: Any, limit: int = 26) -> str:
    """把任意长度的消息压成一行短文本，供状态条显示。

    服务器返回的长消息（例如签到被拒的英文说明）直接塞进状态条时，会被
    裁掉开头、只剩尾部片段，看起来像乱码。统一截断更清晰，完整内容仍在
    弹窗与日志里。
    """
    compact = " ".join(str(text if text is not None else "").split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "…"


# test3.py 在"已结束的点名不能用学生账号补签"时会返回一段英文说明。这里只在
# UI 层做中文替换，不修改 test3.py 本身。键统一小写，匹配时把正文转小写。
_MESSAGE_ZH = (
    (
        "student token cannot rewrite an ended attendance record",
        "该点名已结束，学生账号无法补签这条记录；如需补签需要教师/管理员权限。",
    ),
    (
        "cannot rewrite an already persisted ended/absent row",
        "该点名已结束，学生账号无法改写已存档的签到记录。",
    ),
    (
        "server-issued teacher/admin session",
        "需要教师/管理员权限的会话。",
    ),
)


def _friendly(message: Any) -> str:
    """把少数英文说明翻成中文，便于阅读；未匹配则原样返回。"""
    text = str(message if message is not None else "")
    lowered = text.lower()
    for needle, chinese in _MESSAGE_ZH:
        if needle in lowered:
            return chinese
    return text


def _round_bg(widget: Any, bg: tuple, border: tuple | None = None, radius: float = 10) -> None:
    """给任意控件加一层圆角背景（画在其自身背景之上、内容之下）。"""
    widget._rb_color = Color(*bg)  # type: ignore[attr-defined]
    widget._rb = RoundedRectangle(radius=[radius] * 4)  # type: ignore[attr-defined]
    with widget.canvas.before:
        widget._rb_color  # type: ignore[attr-defined]
        widget._rb  # type: ignore[attr-defined]
        if border is not None:
            widget._rborder_color = Color(*border)  # type: ignore[attr-defined]
            widget._rborder = Line(  # type: ignore[attr-defined]
                rounded_rectangle=(0, 0, 0, 0, radius), width=dp(1)
            )
            widget._rborder_color  # type: ignore[attr-defined]
            widget._rborder  # type: ignore[attr-defined]

    def _sync(*_args: Any) -> None:
        widget._rb.pos = widget.pos  # type: ignore[attr-defined]
        widget._rb.size = widget.size  # type: ignore[attr-defined]
        if border is not None:
            widget._rborder.rounded_rectangle = (  # type: ignore[attr-defined]
                widget.x,
                widget.y,
                widget.width,
                widget.height,
                radius,
            )

    widget.bind(pos=_sync, size=_sync)
    _sync()


class RoundButton(Button):
    """圆角按钮：fill（实底白字）/ outline（白底描边，文字用 bg 或显式 fg）。"""

    def __init__(
        self,
        text: str = "",
        bg: tuple = C["primary"],
        fg: tuple | None = None,
        radius: float = 12,
        outline: bool = False,
        **kw: Any,
    ) -> None:
        kw.setdefault("background_normal", "")
        kw.setdefault("background_down", "")
        kw.setdefault("background_disabled_normal", "")
        kw.setdefault("background_color", (0, 0, 0, 0))
        kw.setdefault("font_name", FONT_NAME)
        kw.setdefault("halign", "center")
        kw.setdefault("valign", "middle")
        # 文本永远限制在按钮内部并自动省略：否则长文案会溢出到按钮之外，
        # 被相邻按钮的不透明背景盖住，看起来像"最后一个字被切掉"。
        kw.setdefault("shorten", True)
        kw.setdefault("shorten_from", "right")
        super().__init__(**kw)
        self.text = text
        self._fill = bg
        self._fg = fg if fg is not None else C["white"]
        self._fg_given = fg is not None
        self._outline = outline
        self._radius = radius
        with self.canvas.before:
            self._bg_color = Color(*bg)
            self._bg = RoundedRectangle(radius=[dp(radius)] * 4)
            if outline:
                self._border_color = Color(*bg)
                self._border = Line(
                    rounded_rectangle=(0, 0, 0, 0, dp(radius)), width=dp(1.1)
                )
        self.bind(pos=self._update_canvas, size=self._update_canvas)
        self.bind(disabled=self._update_state)
        self.bind(size=self._limit_text)
        self._update_canvas()
        self._update_state()
        self._limit_text()

    def _limit_text(self, *_args: Any) -> None:
        self.text_size = (max(dp(1), self.width - dp(10)), self.height)

    def _update_canvas(self, *_args: Any) -> None:
        self._bg.pos = self.pos
        self._bg.size = self.size
        if self._outline and hasattr(self, "_border"):
            self._border.rounded_rectangle = (
                self.x,
                self.y,
                self.width,
                self.height,
                dp(self._radius),
            )

    def _update_state(self, *_args: Any) -> None:
        if self.disabled:
            self._bg_color.rgba = C["chip_gray"]
            if self._outline and hasattr(self, "_border_color"):
                self._border_color.rgba = C["line"]
            self.color = C["white"]
            return
        self._bg_color.rgba = C["card"] if self._outline else self._fill
        if self._outline and hasattr(self, "_border_color"):
            self._border_color.rgba = self._fill
        if self._outline:
            # 描边按钮：显式传了 fg 就用 fg；没传才退化成用 bg 当文字色。
            # 直接用 _fill 会导致 bg=白色（card）的按钮变成白字白底而"消失"。
            self.color = self._fg if self._fg_given else self._fill
        else:
            self.color = self._fg


class Card(BoxLayout):
    """白色圆角卡片（带浅描边）。"""

    def __init__(self, **kw: Any) -> None:
        kw.setdefault("orientation", "vertical")
        kw.setdefault("padding", dp(12))
        kw.setdefault("spacing", dp(8))
        super().__init__(**kw)
        with self.canvas.before:
            Color(*C["card"])
            self._bg = RoundedRectangle(radius=[dp(14)] * 4)
            Color(*C["line"])
            self._border = Line(
                rounded_rectangle=(0, 0, 0, 0, dp(14)), width=dp(1)
            )
        self.bind(pos=self._update_canvas, size=self._update_canvas)
        self._update_canvas()

    def _update_canvas(self, *_args: Any) -> None:
        self._bg.pos = self.pos
        self._bg.size = self.size
        self._border.rounded_rectangle = (
            self.x,
            self.y,
            self.width,
            self.height,
            dp(14),
        )


class Chip(Label):
    """小圆角徽章：类型/状态/签到状态。

    宽度由文本的自然尺寸（texture_size）反推，并且**不设置 text_size**，
    因此文字永远不会折行、也不会在徽章内上下错位。

    旧实现按 `len(text) * sp(11)` 估算宽度，估算偏小时文字会折成两行，
    塞进 20dp 高的徽章里就出现了"上缘残字 + 文字挤在一起"的现象。
    """

    def __init__(
        self, text: str, bg: tuple, fg: tuple = C["white"], **kw: Any
    ) -> None:
        # 尺寸在实例化时用 dp() 计算（模块导入阶段窗口尚未建立，
        # 类属性位置调用 dp() 拿不到真实密度）
        self.pad_x = dp(16)
        self.chip_height = dp(23)
        kw.setdefault("font_name", FONT_NAME)
        kw.setdefault("font_size", sp(11.5))
        kw.setdefault("bold", True)
        kw.setdefault("color", fg)
        kw.setdefault("size_hint", (None, None))
        kw.setdefault("halign", "center")
        kw.setdefault("valign", "middle")
        super().__init__(text=str(text), **kw)
        with self.canvas.before:
            self._color = Color(*bg)
            self._bg = RoundedRectangle(radius=[dp(11)] * 4)
        self.bind(pos=self._update_canvas, size=self._update_canvas)
        self.bind(texture_size=self._sync_size)
        self._sync_size()

    def _sync_size(self, *_args: Any) -> None:
        width = self.texture_size[0]
        if width <= 0:  # 纹理尚未生成时先给一个保守宽度
            width = sp(11.5) * max(1, len(self.text))
        self.size = (width + self.pad_x, self.chip_height)

    def _update_canvas(self, *_args: Any) -> None:
        self._bg.pos = self.pos
        self._bg.size = self.size


class ChineseSpinnerOption(SpinnerOption):
    """Spinner 下拉选项：中文字体 + 适配行高 + 超长文案省略。"""

    def __init__(self, **kw: Any) -> None:
        kw.setdefault("font_name", FONT_NAME)
        kw.setdefault("font_size", sp(14))
        kw.setdefault("height", dp(48))
        kw.setdefault("size_hint_y", None)
        kw.setdefault("halign", "center")
        kw.setdefault("valign", "middle")
        kw.setdefault("shorten", True)
        kw.setdefault("shorten_from", "right")
        super().__init__(**kw)
        self.bind(size=self._limit_text)
        self._limit_text()

    def _limit_text(self, *_args: Any) -> None:
        self.text_size = (max(dp(1), self.width - dp(16)), self.height)


class FlatSpinner(Spinner):
    """扁平化下拉选择器。

    Kivy 原生 Spinner 用深灰渐变贴图做底，在浅色卡片里非常突兀；而且它的
    文本不设边界，课程名一长就会横向溢出到控件之外（压住左侧"课程"标签，
    并在右侧被按钮盖住）。

    这里改为：透明原生底 + 自绘浅色圆角框；文本限制在框内并自动省略。
    """

    def __init__(self, **kw: Any) -> None:
        kw.setdefault("font_name", FONT_NAME)
        kw.setdefault("option_cls", ChineseSpinnerOption)
        kw.setdefault("background_normal", "")
        kw.setdefault("background_down", "")
        kw.setdefault("background_disabled_normal", "")
        kw.setdefault("background_color", (0, 0, 0, 0))
        kw.setdefault("color", C["text"])
        kw.setdefault("font_size", sp(14))
        kw.setdefault("halign", "center")
        kw.setdefault("valign", "middle")
        kw.setdefault("shorten", True)
        kw.setdefault("shorten_from", "right")
        super().__init__(**kw)
        with self.canvas.before:
            Color(*C["bg"])
            self._sk_bg = RoundedRectangle(radius=[dp(10)] * 4)
            Color(*C["line"])
            self._sk_line = Line(
                rounded_rectangle=(0, 0, 0, 0, dp(10)), width=dp(1)
            )
        self.bind(pos=self._update_skin, size=self._update_skin)
        self.bind(size=self._limit_text)
        self._update_skin()
        self._limit_text()

    def _update_skin(self, *_args: Any) -> None:
        self._sk_bg.pos = self.pos
        self._sk_bg.size = self.size
        self._sk_line.rounded_rectangle = (
            self.x,
            self.y,
            self.width,
            self.height,
            dp(10),
        )

    def _limit_text(self, *_args: Any) -> None:
        self.text_size = (max(dp(1), self.width - dp(14)), self.height)


def mobile_spinner(**kw: Any) -> Spinner:
    """保持旧调用点不变，统一返回扁平化 Spinner。"""
    return FlatSpinner(**kw)


def flat_input(hint: str = "", password: bool = False, **kw: Any) -> TextInput:
    """统一输入框样式：浅底 + 圆角描边。

    必须给定显式高度：TextInput 放在以 minimum_height 撑高的卡片里时，
    若不给高度会被压成很矮的一条，文字显示不全。
    """
    kw.setdefault("font_name", FONT_NAME)
    kw.setdefault("font_size", sp(15))
    kw.setdefault("multiline", False)
    kw.setdefault("write_tab", False)
    kw.setdefault("hint_text", hint)
    kw.setdefault("hint_text_color", (0.60, 0.64, 0.71, 1))
    kw.setdefault("password", password)
    kw.setdefault("background_normal", "")
    kw.setdefault("background_active", "")
    kw.setdefault("background_disabled_normal", "")
    # 关掉 TextInput 自带的方角底色，改由 _round_bg 画圆角底
    kw.setdefault("background_color", (0, 0, 0, 0))
    kw.setdefault("foreground_color", C["text"])
    kw.setdefault("cursor_color", C["primary"])
    kw.setdefault("padding", [dp(12), dp(12), dp(12), dp(10)])
    kw.setdefault("size_hint_y", None)
    kw.setdefault("height", dp(46))
    widget = TextInput(**kw)
    _round_bg(widget, C["bg"], C["line"], dp(10))
    return widget


# ---------------------------------------------------------------------------
# 会话层：登录 / 记住登录 / 服务构建（复用 test3.py 全部服务类）
# ---------------------------------------------------------------------------

CONFIG_TEMPLATE = json.dumps(
    {
        "version": 2,
        "timeout": 30.0,
        "retries": 3,
        "backoff": 1.0,
        "activity_page_size": 100,
        "verify_delay": 1.5,
        "history_file": "ulearning_history.json",
        "default_location": "",
        "default_coord_system": "bd09",
        "default_address": "",
    },
    ensure_ascii=False,
    indent=2,
)


class MobileSession:
    """登录会话 + session.json 持久化（应用私有目录）。"""

    def __init__(self, app_dir: Path) -> None:
        self.app_dir = app_dir
        self.app_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = app_dir / "session.json"
        self.config_path = app_dir / "ulearning-cmd.json"
        self.config = self._load_config()
        self.token = ""
        self.username = ""
        self.user_id = 0
        self.role_id = 0
        self.client: HttpClient | None = None
        self.courses_api: CourseService | None = None
        self.activities_api: ActivityService | None = None
        self.attendance_api: AttendanceService | None = None
        self._load_saved_session()

    def _load_config(self) -> AppConfig:
        if not self.config_path.exists():
            self.config_path.write_text(
                CONFIG_TEMPLATE + "\n", encoding="utf-8"
            )
        return load_config(self.config_path)

    def _load_saved_session(self) -> None:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return
        if not isinstance(data, dict):
            return
        self.username = str(data.get("username", "") or "")
        self.token = str(data.get("token", "") or "")
        try:
            self.user_id = int(data.get("user_id", 0) or 0)
            self.role_id = int(data.get("role_id", 0) or 0)
        except (TypeError, ValueError):
            self.user_id = 0
            self.role_id = 0

    @property
    def logged_in(self) -> bool:
        return bool(self.token and self.user_id)

    def _save_session(self) -> None:
        self.state_path.write_text(
            json.dumps(
                {
                    "username": self.username,
                    "token": self.token,
                    "user_id": self.user_id,
                    "role_id": self.role_id,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def _build_services(self) -> None:
        if not self.logged_in:
            raise AuthenticationError("请先登录")
        self.client = HttpClient(
            token=self.token,
            timeout=self.config.timeout,
            retries=self.config.retries,
            backoff=self.config.backoff,
        )
        self.courses_api = CourseService(self.client, self.config.lms_base_url)
        self.activities_api = ActivityService(
            self.client,
            application_base_url=self.config.application_base_url,
            lms_base_url=self.config.lms_base_url,
            user_id=self.user_id,
            page_size=self.config.activity_page_size,
        )
        self.attendance_api = AttendanceService(
            self.client,
            self.activities_api,
            application_base_url=self.config.application_base_url,
            user_id=self.user_id,
            verify_delay=self.config.verify_delay,
            history_file=self.app_dir / self.config.history_file,
            allow_ended=True,
        )

    def restore(self) -> bool:
        if not self.logged_in:
            return False
        try:
            self._build_services()
            return True
        except Exception:
            self.clear_session()
            return False

    def login(self, username: str, password: str, remember: bool) -> None:
        result = LoginService(
            self.config.application_base_url, timeout=self.config.timeout
        ).login(username.strip(), password)
        self.token = result.token
        self.username = username.strip()
        self.user_id = result.user_id
        self.role_id = result.role_id
        self._build_services()
        if remember:
            self._save_session()
        else:
            self._delete_saved_session()

    def clear_session(self) -> None:
        self.token = ""
        self.username = ""
        self.user_id = 0
        self.role_id = 0
        self.client = None
        self.courses_api = None
        self.activities_api = None
        self.attendance_api = None

    def _delete_saved_session(self) -> None:
        try:
            self.state_path.unlink(missing_ok=True)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 登录页
# ---------------------------------------------------------------------------


class LoginScreen(Screen):
    def __init__(self, controller: "MobileApp", **kw: Any) -> None:
        super().__init__(**kw)
        self.controller = controller
        root = BoxLayout(orientation="vertical")
        with root.canvas.before:
            Color(*C["bg"])
            Rectangle(pos=root.pos, size=root.size)
        root.bind(
            pos=lambda w, _s: setattr(
                root.canvas.before.children[-1], "pos", _s
            ),
            size=lambda w, _s: setattr(
                root.canvas.before.children[-1], "size", _s
            ),
        )

        # 顶部品牌区
        brand = BoxLayout(
            orientation="vertical",
            padding=[dp(24), dp(46), dp(24), dp(28)],
            spacing=dp(6),
            size_hint_y=None,
            height=dp(150),
        )
        with brand.canvas.before:
            Color(*C["primary"])
            Rectangle(pos=brand.pos, size=brand.size)
        brand.bind(
            pos=lambda w, _s: setattr(
                brand.canvas.before.children[-1], "pos", _s
            ),
            size=lambda w, _s: setattr(
                brand.canvas.before.children[-1], "size", _s
            ),
        )
        brand.add_widget(
            L("签到器", 30, C["white"], True, halign="left")
        )
        brand.add_widget(
            L("DGUT 课程 · 点名 · 一键签到", 13, (1, 1, 1, 0.82), halign="left")
        )
        root.add_widget(brand)

        # 表单卡片
        body = BoxLayout(
            orientation="vertical", padding=[dp(20), dp(8), dp(20), dp(16)]
        )
        card = Card(spacing=dp(12), size_hint_y=None, height=dp(352))
        card.add_widget(
            L("账号登录", 17, C["text"], True, halign="left",
              size_hint_y=None, height=dp(28))
        )
        card.add_widget(
            L("使用学校优学院账号登录", 12.5, C["text_sub"], halign="left",
              size_hint_y=None, height=dp(20))
        )
        self.username = flat_input(
            "学号 / 账号", size_hint_y=None, height=dp(46)
        )
        card.add_widget(self.username)
        self.password = flat_input(
            "密码", password=True, size_hint_y=None, height=dp(46)
        )
        card.add_widget(self.password)
        remember_row = BoxLayout(spacing=dp(8), size_hint_y=None, height=dp(28))
        remember_wrap = FloatLayout(
            size_hint=(None, None), size=(dp(24), dp(24)),
            pos_hint={"center_y": 0.5},
        )
        _round_bg(remember_wrap, C["bg"], C["line"], dp(7))
        self.remember = CheckBox(
            active=True,
            color=C["primary"],
            size_hint=(None, None),
            size=(dp(20), dp(20)),
            pos_hint={"center_x": 0.5, "center_y": 0.5},
        )
        remember_wrap.add_widget(self.remember)
        remember_row.add_widget(remember_wrap)
        remember_row.add_widget(
            L("记住本次登录，下次自动登录", 13, C["text_sub"], halign="left")
        )
        card.add_widget(remember_row)
        self.login_button = RoundButton(
            "登 录", bg=C["primary"], radius=14, size_hint_y=None, height=dp(48)
        )
        self.login_button.bind(on_release=lambda _b: self.submit())
        card.add_widget(self.login_button)
        self.message = L(
            "", 12.5, C["danger"], halign="left", size_hint_y=None, height=dp(36)
        )
        card.add_widget(self.message)
        body.add_widget(card)
        root.add_widget(body)

        # 底部提示
        root.add_widget(
            L(
                "学生账号仅能执行本人签到\n定位签到位置由服务端校验",
                12,
                C["text_sub"],
                halign="center",
                size_hint_y=None,
                height=dp(40),
            )
        )
        self.add_widget(root)

    def set_message(self, text: str) -> None:
        self.message.text = text or ""

    def submit(self) -> None:
        if not self.username.text.strip() or not self.password.text:
            self.message.text = "请输入账号和密码"
            return
        self.message.text = "正在登录..."
        self.login_button.disabled = True

        def operation() -> Any:
            self.controller.session.login(
                self.username.text.strip(),
                self.password.text,
                bool(self.remember.active),
            )

        def done(_result: Any, error: Exception | None) -> None:
            self.login_button.disabled = False
            if error is not None:
                self.message.text = f"登录失败：{error}"
                return
            self.password.text = ""
            self.message.text = ""
            self.controller.show_main()

        self.controller.run_async(operation, done)


# ---------------------------------------------------------------------------
# 主界面
# ---------------------------------------------------------------------------


class NavButton(Button):
    """底部导航按钮：选中时主色文字 + 顶部指示条。"""

    def __init__(self, text: str, **kw: Any) -> None:
        kw.setdefault("background_normal", "")
        kw.setdefault("background_down", "")
        kw.setdefault("background_disabled_normal", "")
        kw.setdefault("background_color", (0, 0, 0, 0))
        kw.setdefault("font_name", FONT_NAME)
        kw.setdefault("font_size", sp(13))
        super().__init__(text=text, **kw)
        self._selected = False
        with self.canvas.before:
            self._bar_color = Color(0, 0, 0, 0)
            self._bar = Rectangle(pos=(self.x, self.top - dp(3)), size=(self.width, dp(3)))
        self.bind(pos=self._update_canvas, size=self._update_canvas)
        self._update_canvas()

    def _update_canvas(self, *_args: Any) -> None:
        self._bar.pos = (self.x, self.top - dp(3))
        self._bar.size = (self.width, dp(3))

    def set_selected(self, flag: bool) -> None:
        self._selected = bool(flag)
        self.color = C["primary"] if flag else C["text_sub"]
        self.bold = bool(flag)
        self._bar_color.rgba = C["primary"] if flag else (0, 0, 0, 0)


class MainScreen(Screen):
    def __init__(self, controller: "MobileApp", **kw: Any) -> None:
        super().__init__(**kw)
        self.controller = controller
        self.courses: list[Course] = []
        self.activities: list[dict[str, Any]] = []
        self.selected_activity: dict[str, Any] | None = None
        self.selection_snapshot: dict[str, Any] | None = None
        self.active_only = False
        self._entered = False
        self._pages: dict[str, BoxLayout] = {}
        self._nav_buttons: dict[str, NavButton] = {}

        root = BoxLayout(orientation="vertical")
        with root.canvas.before:
            Color(*C["bg"])
            Rectangle(pos=root.pos, size=root.size)
        root.bind(
            pos=lambda w, _s: setattr(
                root.canvas.before.children[-1], "pos", _s
            ),
            size=lambda w, _s: setattr(
                root.canvas.before.children[-1], "size", _s
            ),
        )

        # ---- 顶部头部（主色） ----
        header = BoxLayout(
            orientation="vertical",
            padding=[dp(16), dp(14), dp(14), dp(10)],
            spacing=dp(2),
            size_hint_y=None,
            height=dp(86),
        )
        with header.canvas.before:
            Color(*C["primary"])
            Rectangle(pos=header.pos, size=header.size)
        header.bind(
            pos=lambda w, _s: setattr(
                header.canvas.before.children[-1], "pos", _s
            ),
            size=lambda w, _s: setattr(
                header.canvas.before.children[-1], "size", _s
            ),
        )
        row1 = BoxLayout(spacing=dp(10))
        row1.add_widget(L("签到器", 19, C["white"], True, halign="left"))
        logout_btn = RoundButton(
            "切换用户",
            bg=(1, 1, 1, 0.18),
            fg=C["white"],
            radius=15,
            size_hint=(None, None),
            size=(dp(88), dp(30)),
            font_size=sp(12.5),
        )
        logout_btn.bind(
            on_release=lambda _b: self.controller.logout("已退出登录")
        )
        row1.add_widget(logout_btn)
        header.add_widget(row1)
        self.user_label = short_label("", 12.5, (1, 1, 1, 0.85))
        header.add_widget(self.user_label)
        root.add_widget(header)

        # ---- 课程选择卡片 ----
        # 两行布局：上行"课程 + 下拉框 + 刷新"，下行用一行小字显示完整课程名。
        # 课程名往往很长（如 "2025 | 【2026秋】卓越工程大师讲座"），单行下拉框
        # 只能显示省略号，看不出是哪门课，因此单独给一行完整名称。
        course_card = Card(
            orientation="vertical",
            padding=[dp(12), dp(10)],
            spacing=dp(6),
            size_hint_y=None,
            height=dp(108),
        )
        course_row = BoxLayout(
            spacing=dp(8), size_hint_y=None, height=dp(42)
        )
        course_row.add_widget(
            L(
                "课程",
                13.5,
                C["text"],
                True,
                size_hint_x=None,
                width=dp(34),
                halign="left",
            )
        )
        self.course_spinner = mobile_spinner(
            text="请先刷新课程",
            values=(),
            size_hint_x=1,
        )
        course_row.add_widget(self.course_spinner)
        refresh_btn = RoundButton(
            "刷新",
            bg=C["teal"],
            radius=10,
            size_hint_x=None,
            width=dp(60),
            font_size=sp(13.5),
        )
        refresh_btn.bind(on_release=lambda _b: self.refresh_courses())
        course_row.add_widget(refresh_btn)
        course_card.add_widget(course_row)
        self.course_full = L(
            "尚未加载课程，点击「刷新」获取课程列表",
            11.5,
            C["text_sub"],
            halign="left",
            size_hint_y=None,
            height=dp(38),
        )
        course_card.add_widget(self.course_full)
        # 绑定放在 course_full 创建之后，避免回调早于属性初始化而报错
        self.course_spinner.bind(text=self.course_changed)
        root.add_widget(course_card)

        # ---- 状态条 ----
        status_wrap = BoxLayout(
            padding=[dp(10), dp(2), dp(10), dp(4)], size_hint_y=None, height=dp(34)
        )
        self._status_color = Color(*STATUS_COLORS["info"][0])
        self.status = L(
            "准备就绪",
            13,
            STATUS_COLORS["info"][1],
            halign="left",
            shorten=True,
            shorten_from="right",
        )
        with self.status.canvas.before:
            self._status_color
            RoundedRectangle(radius=[dp(9)] * 4, pos=self.status.pos, size=self.status.size)
        self.status.bind(
            pos=lambda w, _s: setattr(
                self.status.canvas.before.children[-1], "pos", _s
            ),
            size=lambda w, _s: setattr(
                self.status.canvas.before.children[-1], "size", _s
            ),
        )
        status_wrap.add_widget(self.status)
        root.add_widget(status_wrap)

        # ---- 内容区（三页切换） ----
        self.content = BoxLayout(padding=[dp(10), dp(2), dp(10), dp(4)])
        root.add_widget(self.content)
        self._pages["activity"] = self._build_activity_page()
        self._pages["tools"] = self._build_tools_page()
        self._pages["log"] = self._build_log_page()

        # ---- 底部导航 ----
        nav = BoxLayout(size_hint_y=None, height=dp(54), spacing=0)
        with nav.canvas.before:
            Color(*C["card"])
            nav_bg = Rectangle(pos=nav.pos, size=nav.size)
        nav.bind(
            pos=lambda w, _s: setattr(nav.canvas.before.children[-1], "pos", _s),
            size=lambda w, _s: setattr(nav.canvas.before.children[-1], "size", _s),
        )
        for key, text in (("activity", "活动"), ("tools", "工具"), ("log", "日志")):
            btn = NavButton(text)
            btn.bind(on_release=lambda _b, k=key: self.switch_page(k))
            self._nav_buttons[key] = btn
            nav.add_widget(btn)
        root.add_widget(nav)

        self.add_widget(root)
        self.switch_page("activity")

    # ------------------------------------------------------------------
    # 页面构建
    # ------------------------------------------------------------------

    def _build_activity_page(self) -> BoxLayout:
        page = BoxLayout(orientation="vertical", spacing=dp(8))

        action = BoxLayout(spacing=dp(8), size_hint_y=None, height=dp(40))
        btn_all = RoundButton("查询全部活动", bg=C["primary"], radius=12)
        btn_all.bind(on_release=lambda _b: self.query_activities("all"))
        action.add_widget(btn_all)
        btn_sel = RoundButton(
            "查询选人", bg=C["purple"], outline=True, radius=12
        )
        btn_sel.bind(on_release=lambda _b: self.query_activities("selection"))
        action.add_widget(btn_sel)
        check_row = BoxLayout(
            spacing=dp(4), size_hint=(None, 1), width=dp(94)
        )
        # CheckBox 自带的图是"白底 + 浅灰描边"，放在纯白卡片上几乎看不见；
        # 套一层浅灰圆角框，未勾选状态也能一眼看到。
        check_wrap = FloatLayout(
            size_hint=(None, None), size=(dp(26), dp(26))
        )
        _round_bg(check_wrap, C["bg"], C["line"], dp(7))
        self.active_check = CheckBox(
            active=self.active_only,
            color=C["primary"],
            size_hint=(None, None),
            size=(dp(22), dp(22)),
            pos_hint={"center_x": 0.5, "center_y": 0.5},
        )
        self.active_check.bind(active=self._active_filter_changed)
        check_wrap.add_widget(self.active_check)
        check_row.add_widget(check_wrap)
        check_row.add_widget(
            L("只看进行中", 11.5, C["text_sub"], halign="left")
        )
        action.add_widget(check_row)
        page.add_widget(action)

        selected_card = Card(
            orientation="horizontal",
            padding=[dp(12), dp(8)],
            size_hint_y=None,
            height=dp(46),
        )
        self.selected_label = short_label("未选择活动", 13, C["text_sub"])
        selected_card.add_widget(self.selected_label)
        page.add_widget(selected_card)

        scroll = ScrollView()
        self.activity_box = BoxLayout(
            orientation="vertical",
            spacing=dp(8),
            padding=[0, dp(2), 0, dp(4)],
            size_hint_y=None,
        )
        self.activity_box.bind(
            minimum_height=lambda _w, _h: setattr(
                self.activity_box, "height", _h
            )
        )
        scroll.add_widget(self.activity_box)
        page.add_widget(scroll)

        sign_row = BoxLayout(spacing=dp(8), size_hint_y=None, height=dp(46))
        btn_sign = RoundButton("对选中点名签到", bg=C["primary"], radius=13)
        btn_sign.bind(on_release=lambda _b: self.sign_selected())
        sign_row.add_widget(btn_sign)
        btn_auto = RoundButton("一键签到当前课程", bg=C["teal"], radius=13)
        btn_auto.bind(on_release=lambda _b: self.auto_sign_current_course())
        sign_row.add_widget(btn_auto)
        page.add_widget(sign_row)
        return page

    def _build_tools_page(self) -> BoxLayout:
        page = BoxLayout(orientation="vertical")
        scroll = ScrollView()
        box = BoxLayout(
            orientation="vertical",
            spacing=dp(10),
            padding=[0, dp(2), 0, dp(8)],
            size_hint_y=None,
        )
        box.bind(minimum_height=lambda _w, _h: setattr(box, "height", _h))

        # 同上：外层 box 也是 minimum_height 撑高，卡片必须 size_hint_y=None
        # 并绑定 minimum_height，否则高度为 0、内容整体溢出。
        form = Card(spacing=dp(8), size_hint_y=None)
        form.bind(minimum_height=lambda w, h: setattr(w, "height", h))
        form.add_widget(
            L("签到参数", 15, C["text"], True, halign="left",
              size_hint_y=None, height=dp(26))
        )
        form.add_widget(
            L("签到位置（经度,纬度）", 12.5, C["text_sub"], halign="left",
              size_hint_y=None, height=dp(20))
        )
        self.location = flat_input("例如 113.879022,22.901269")
        form.add_widget(self.location)
        coord_row = BoxLayout(spacing=dp(8), size_hint_y=None, height=dp(46))
        coord_row.add_widget(
            L("坐标系", 12.5, C["text_sub"], halign="left",
              size_hint_x=None, width=dp(60))
        )
        self.coord = mobile_spinner(
            text="bd09", values=("gcj02", "wgs84", "bd09"), size_hint_x=1
        )
        coord_row.add_widget(self.coord)
        form.add_widget(coord_row)
        form.add_widget(
            L("地址（可选）", 12.5, C["text_sub"], halign="left",
              size_hint_y=None, height=dp(20))
        )
        self.address = flat_input("例如：教学楼 A101")
        form.add_widget(self.address)
        form.add_widget(
            L("签到码（教师提供时填写）", 12.5, C["text_sub"], halign="left",
              size_hint_y=None, height=dp(20))
        )
        self.attendance_code = flat_input("签到码")
        form.add_widget(self.attendance_code)
        box.add_widget(form)

        tools = Card(spacing=dp(8), size_hint_y=None)
        tools.bind(minimum_height=lambda w, h: setattr(w, "height", h))
        tools.add_widget(
            L("工具", 15, C["text"], True, halign="left",
              size_hint_y=None, height=dp(26))
        )
        grid = GridLayout(cols=2, spacing=dp(8), size_hint_y=None)
        grid.bind(
            minimum_height=lambda _w, _h: setattr(grid, "height", _h)
        )
        tool_specs = (
            ("诊断", C["primary"], False, self.query_diagnostic),
            ("选人详情", C["purple"], False, self.query_selection_evidence),
            ("记录选人快照", C["teal"], True, self.record_selection_snapshot),
            ("对比选人快照", C["teal"], True, self.compare_selection_snapshot),
            ("刷新课程", C["teal"], False, self.refresh_courses),
            ("打开官方页面", C["text_sub"], True, self.open_official_page),
        )
        for text, color, outline, handler in tool_specs:
            btn = RoundButton(
                text,
                bg=color,
                outline=outline,
                radius=11,
                size_hint_y=None,
                height=dp(42),
            )
            btn.bind(on_release=lambda _b, h=handler: h())
            grid.add_widget(btn)
        tools.add_widget(grid)
        box.add_widget(tools)

        box.add_widget(
            L(
                "定位签到：优先使用服务端返回的中心点，并加 3~10 米随机偏移；\n"
                "手动坐标仅在活动无中心点时生效。",
                12,
                C["text_sub"],
                halign="left",
                size_hint_y=None,
                height=dp(60),
            )
        )
        page.add_widget(scroll)
        scroll.add_widget(box)
        return page

    def _build_log_page(self) -> BoxLayout:
        page = BoxLayout(orientation="vertical", spacing=dp(8))
        head = BoxLayout(spacing=dp(8), size_hint_y=None, height=dp(40))
        head.add_widget(L("请求与签到日志", 15, C["text"], True, halign="left"))
        clear_btn = RoundButton(
            "清空日志",
            bg=C["card"],
            fg=C["danger"],
            outline=True,
            radius=12,
            size_hint_x=None,
            width=dp(96),
        )
        clear_btn.bind(on_release=lambda _b: self.clear_log())
        head.add_widget(clear_btn)
        page.add_widget(head)
        self.log_output = TextInput(
            text="",
            readonly=True,
            multiline=True,
            font_name=FONT_NAME,
            font_size=sp(12.5),
            background_normal="",
            background_active="",
            background_disabled_normal="",
            background_color=C["dark"],
            foreground_color=C["dark_text"],
            cursor_blink=False,
            cursor_color=C["dark"],
        )
        page.add_widget(self.log_output)
        return page

    def switch_page(self, key: str) -> None:
        for k, btn in self._nav_buttons.items():
            btn.set_selected(k == key)
        self.content.clear_widgets()
        self.content.add_widget(self._pages[key])

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def on_pre_enter(self) -> None:
        session = self.controller.session
        self.user_label.text = (
            f"用户 {session.username or '未登录'}"
            f" · ID {session.user_id}"
            f" · 角色 {session.role_id}"
        )
        if not self.location.text.strip():
            self.location.text = self.controller.session.config.default_location
        if not self.address.text.strip():
            self.address.text = self.controller.session.config.default_address
        if self.coord.text == "bd09":
            self.coord.text = (
                self.controller.session.config.default_coord_system or "bd09"
            )
        if not self._entered:
            self._entered = True
            self.write_log("已进入主界面", {"user": session.username, "id": session.user_id})
            self.refresh_courses()

    def reset_state(self) -> None:
        self.courses = []
        self.activities = []
        self.selected_activity = None
        self.active_only = False
        self.active_check.active = False
        self.course_spinner.values = ()
        self.course_spinner.text = "请先刷新课程"
        self.course_full.text = "尚未加载课程，点击「刷新」获取课程列表"
        self.selected_label.text = "未选择活动"
        self.activity_box.clear_widgets()
        self._entered = False

    # ------------------------------------------------------------------
    # 状态与日志
    # ------------------------------------------------------------------

    def set_status(self, text: str, kind: str = "info") -> None:
        """更新状态条。

        长文本（例如服务器返回的整段英文拒绝说明）会被压成一行短摘要：
        直接塞进来的话会被裁掉开头、只剩尾部片段，看起来像乱码。完整内容
        仍然保留在弹窗和日志里。
        """
        bg, fg = STATUS_COLORS.get(kind, STATUS_COLORS["info"])
        self._status_color.rgba = bg
        self.status.color = fg
        self.status.text = _brief(text)

    def write_log(self, message: str, payload: Any = None) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{stamp}] {message}"
        if payload is not None:
            try:
                line += "\n" + json.dumps(
                    payload, ensure_ascii=False, indent=2, default=str
                )
            except (TypeError, ValueError):
                line += "\n" + str(payload)
        text = self.log_output.text
        if text:
            text += "\n"
        text += line
        if len(text) > 50000:
            text = text[-50000:]
        self.log_output.text = text
        self.log_output.cursor = (0, len(self.log_output._lines))
        self.log_output.scroll_y = 0

    def clear_log(self) -> None:
        self.log_output.text = ""
        self.write_log("日志已清空")

    def _show_message(self, title: str, message: str) -> None:
        """结果弹窗：高度随文本量自适应，正文可滚动。

        原来固定 dp(210) 高，服务器返回的长说明（如整段英文拒绝原因）会被
        截断；现在按行数估算高度，并把正文放进 ScrollView。
        """
        text = str(message if message is not None else "")
        content = BoxLayout(orientation="vertical", padding=dp(14), spacing=dp(12))

        body = Label(
            text=text,
            font_name=FONT_NAME,
            font_size=sp(14),
            # Kivy 默认弹窗背景是深灰底，正文必须用浅色，否则几乎看不见
            color=C["dark_text"],
            halign="left",
            valign="middle",
            size_hint_y=None,
            text_size=(dp(100), None),
        )
        body.bind(width=lambda w, wd: setattr(w, "text_size", (wd, None)))
        body.bind(
            texture_size=lambda w, ts: setattr(w, "height", max(ts[1], dp(20)))
        )
        holder = BoxLayout(orientation="vertical", size_hint_y=None)
        holder.bind(minimum_height=lambda w, _h: setattr(w, "height", _h))
        holder.add_widget(body)

        view = ScrollView(bar_width=dp(2))
        view.add_widget(holder)
        content.add_widget(view)

        btn = RoundButton(
            "知道了", bg=C["primary"], radius=12, size_hint_y=None, height=dp(44)
        )
        content.add_widget(btn)

        lines = 1 + text.count("\n") + len(text) // 20
        height = min(dp(500), max(dp(180), dp(132) + lines * dp(20)))
        popup = Popup(
            title=title,
            title_font=FONT_NAME,
            title_size=sp(16),
            separator_color=C["line"],
            content=content,
            size_hint=(0.88, None),
            height=height,
            auto_dismiss=False,
        )
        btn.bind(on_release=popup.dismiss)
        popup.open()

    def _handle_error(self, action: str, error: Exception | None) -> None:
        if isinstance(error, AuthenticationError):
            self.write_log(f"{action}失败：登录已过期", {"error": str(error)})
            self.controller.logout("登录已过期，请重新登录")
            return
        detail = _friendly(str(error) if error is not None else "请求失败")
        self.write_log(f"{action}失败：{detail}")
        self.set_status(f"{action}失败", "error")
        self._show_message(action, detail)

    # ------------------------------------------------------------------
    # 课程与活动
    # ------------------------------------------------------------------

    def _current_course(self) -> Course | None:
        if not self.courses:
            return None
        try:
            index = list(self.course_spinner.values).index(self.course_spinner.text)
        except ValueError:
            return None
        if 0 <= index < len(self.courses):
            return self.courses[index]
        return None

    def refresh_courses(self) -> None:
        if self.controller.session.courses_api is None:
            self._show_message("刷新课程", "请先登录")
            return
        self.set_status("正在加载课程...", "info")
        self.write_log("开始加载课程")

        def done(value: Any, error: Exception | None) -> None:
            if error is not None:
                self._handle_error("加载课程", error)
                return
            self.courses = list(value or [])
            self.course_spinner.values = tuple(
                f"{c.course_id} | {c.name}" for c in self.courses
            )
            self.set_status(f"已加载 {len(self.courses)} 门课程", "success")
            self.write_log(f"已加载 {len(self.courses)} 门课程")
            if self.courses:
                self.course_spinner.text = self.course_spinner.values[0]

        self.controller.run_async(
            lambda: self.controller.session.courses_api.list_courses(), done
        )

    def course_changed(self, _spinner: Spinner, text: str) -> None:
        if not text or text == "请先刷新课程":
            self.course_full.text = "尚未加载课程，点击「刷新」获取课程列表"
            return
        self.course_full.text = text
        self.write_log(f"选择课程：{text}")
        self.query_activities("all")

    def _active_filter_changed(self, _widget: CheckBox, active: bool) -> None:
        self.active_only = bool(active)
        self.write_log(f"只看进行中：{'开启' if active else '关闭'}")
        self._render_activities()

    def query_activities(self, mode: str) -> None:
        course = self._current_course()
        if course is None or self.controller.session.activities_api is None:
            self._show_message("提示", "请先刷新并选择课程")
            return
        label = "选人活动" if mode == "selection" else "全部活动"
        self.set_status(f"正在查询{label}...", "info")
        self.write_log(f"开始查询{label}：{course.course_id} | {course.name}")

        def done(value: Any, error: Exception | None) -> None:
            if error is not None:
                self._handle_error("查询活动", error)
                return
            activities = list(value or [])
            if mode == "selection":
                activities = [
                    a
                    for a in activities
                    if _as_int(a.get("relationType")) == 4
                ]
            self.activities = activities
            self._render_activities()
            self.set_status(f"查询完成：{len(activities)} 个活动", "success")
            self.write_log(f"查询完成：显示 {len(activities)} 个活动")

        self.controller.run_async(
            lambda: self.controller.session.activities_api.list_activities(course),
            done,
        )

    def _render_activities(self) -> None:
        self.activity_box.clear_widgets()
        self.selected_activity = None
        self.selected_label.text = "未选择活动"
        source = self.activities
        if self.active_only:
            source = [a for a in source if activity_is_active(a)]
        if not source:
            self.activity_box.add_widget(
                L(
                    "暂无活动\n点击「查询全部活动」刷新",
                    13,
                    C["text_sub"],
                    halign="center",
                    size_hint_y=None,
                    height=dp(72),
                )
            )
            return
        for item in source:
            self.activity_box.add_widget(self._activity_card(item))

    def _activity_card(self, activity: Mapping[str, Any]) -> BoxLayout:
        rtype = _as_int(activity.get("relationType"), -1)
        type_name = RELATION_TYPE.get(rtype) or f"未知({rtype})"
        title = str(
            activity.get("title") or activity.get("name") or "未命名活动"
        ).replace("\n", " ")
        status = activity_status(activity)
        signed = (
            self.controller.session.attendance_api.is_signed(activity)
            if self.controller.session.attendance_api is not None
            else attendance_signed(activity)
        )

        type_color = {
            1: C["primary"],
            4: C["purple"],
        }.get(rtype, (0.42, 0.45, 0.50, 1))
        status_bg, status_fg = (
            (C["teal_bg"], C["teal"])
            if status == "进行中"
            else (C["chip_gray"], C["text_sub"])
        )
        if rtype == 4:
            sel_text, sel_selected = selection_state(activity)
            sign_chip = Chip(
                f"{sel_text}", C["purple_bg"] if sel_selected else C["chip_gray"],
                C["purple"] if sel_selected else C["text_sub"],
            )
        else:
            sign_chip = Chip(
                "已签到" if signed else "未签到",
                C["teal_bg"] if signed else C["danger_bg"],
                C["teal"] if signed else C["danger"],
            )

        # 高度必须显式化：activity_box 的高度由 minimum_height 决定，而
        # minimum_height 只统计 size_hint_y=None 的子控件。若卡片保留默认的
        # size_hint_y=1，卡片高度会被算成 0 —— 表现为白色卡片背景不显示、
        # 内部徽章与按钮溢出到卡片之外互相叠压。
        card = Card(spacing=dp(8), size_hint_y=None)
        card.bind(minimum_height=lambda w, h: setattr(w, "height", h))
        chips = BoxLayout(spacing=dp(6), size_hint_y=None, height=dp(24))
        chips.add_widget(Chip(type_name, type_color))
        chips.add_widget(Chip(status, status_bg, status_fg))
        chips.add_widget(sign_chip)
        chips.add_widget(L("", 11, size_hint_x=1))
        card.add_widget(chips)

        title_row = BoxLayout(spacing=dp(6), size_hint_y=None, height=dp(34))
        title_label = short_label(title, 14, C["text"], bold=True)
        title_label.bind(
            on_touch_down=lambda w, touch: (
                self.select_activity(activity)
                if w.collide_point(*touch.pos)
                else None
            )
        )
        title_row.add_widget(title_label)

        choose_btn = RoundButton(
            "选择",
            bg=C["card"],
            fg=C["text_sub"],
            outline=True,
            radius=10,
            size_hint_x=None,
            width=dp(64),
            font_size=sp(12.5),
        )
        choose_btn.bind(
            on_release=lambda _b, it=dict(activity): self.select_activity(it)
        )
        title_row.add_widget(choose_btn)
        if rtype == 1:
            sign_btn = RoundButton(
                "签到",
                bg=C["primary"] if not signed else C["chip_gray"],
                radius=10,
                size_hint_x=None,
                width=dp(64),
                font_size=sp(12.5),
                disabled=bool(signed),
            )
            sign_btn.bind(
                on_release=lambda _b, it=dict(activity): self.sign(it)
            )
            title_row.add_widget(sign_btn)
        card.add_widget(title_row)
        return card

    def select_activity(self, activity: Mapping[str, Any]) -> None:
        self.selected_activity = dict(activity)
        activity_id = str(activity.get("relationId", activity.get("id", "")))
        title = str(activity.get("title") or activity.get("name") or "")
        self.selected_label.text = f"已选：{activity_id} | {title}"
        self.set_status("已选择活动", "info")
        self.write_log(f"选中活动：{activity_id} | {title}")

    # ------------------------------------------------------------------
    # 签到
    # ------------------------------------------------------------------

    def _signin_options(self) -> dict[str, str]:
        return {
            "location": self.location.text.strip(),
            "address": self.address.text.strip(),
            "coord_system": self.coord.text.strip() or "bd09",
            "attendance_code": self.attendance_code.text.strip(),
        }

    def sign_selected(self) -> None:
        if not self.selected_activity:
            self._show_message("签到", "请先在活动列表中选择一个点名活动")
            return
        self.sign(self.selected_activity)

    def sign(self, activity: Mapping[str, Any]) -> None:
        course = self._current_course()
        if course is None or self.controller.session.attendance_api is None:
            self._show_message("签到", "请先刷新并选择课程")
            return
        if _as_int(activity.get("relationType")) != 1:
            self._show_message("签到", "选中的活动不是点名签到活动")
            return
        api = self.controller.session.attendance_api
        if api.is_signed(activity):
            self._show_message("签到", "服务器状态显示已签到")
            return
        ok, reason = AttendanceService.can_sign(activity, allow_ended=True)
        if not ok:
            self._show_message("签到", f"当前不可签到：{reason}")
            return
        activity_id = str(activity.get("relationId", activity.get("id", "")))
        options = self._signin_options()
        self.set_status("正在提交签到...", "info")
        self.write_log(
            f"准备签到：活动 {activity_id}",
            {"course_id": course.course_id, "options": options},
        )

        def operation() -> Any:
            return api.sign(course, activity, **options, allow_ended=True, dry_run=False)

        self.controller.run_async(operation, self._sign_done)

    def _sign_done(self, value: Any, error: Exception | None) -> None:
        if error is not None:
            self._handle_error("签到", error)
            return
        if not isinstance(value, dict):
            self.set_status("签到服务器响应异常", "error")
            return
        success = bool(value.get("success"))
        message = _friendly(value.get("message") or "请求已完成") or "请求已完成"
        self.write_log("签到成功" if success else "签到失败", value)
        self.set_status(
            f"签到成功：{message}" if success else f"签到失败：{message}",
            "success" if success else "error",
        )
        self._show_message("签到成功" if success else "签到失败", message)
        self.switch_page("activity")
        course = self._current_course()
        if course is None or self.controller.session.activities_api is None:
            return

        def refresh_done(refreshed: Any, refresh_error: Exception | None) -> None:
            if refresh_error is not None:
                self.write_log(f"签到后刷新状态失败：{refresh_error}")
                return
            self.activities = list(refreshed or [])
            self._render_activities()
            self.write_log("已重新查询服务器签到状态")

        self.controller.run_async(
            lambda: self.controller.session.activities_api.list_activities(course),
            refresh_done,
        )

    def auto_sign_current_course(self) -> None:
        course = self._current_course()
        session = self.controller.session
        if course is None or session.activities_api is None or session.attendance_api is None:
            self._show_message("一键签到", "请先刷新并选择课程")
            return
        options = self._signin_options()
        self.set_status("正在查找可签到活动...", "info")
        self.write_log(f"一键签到检查：{course.course_id} | {course.name}")

        def operation() -> dict[str, Any]:
            activities = session.activities_api.list_activities(course)
            signable = signable_attendance_activities(
                activities,
                allow_ended=True,
                signed_checker=session.attendance_api.is_signed,
            )
            if not signable:
                return {"kind": "none", "activities": activities}
            if len(signable) > 1:
                return {"kind": "multiple", "activities": activities}
            result = session.attendance_api.sign(
                course, signable[0], **options, allow_ended=True, dry_run=False
            )
            return {"kind": "result", "activities": activities, "result": result}

        def done(value: Any, error: Exception | None) -> None:
            if error is not None:
                self._handle_error("一键签到", error)
                return
            if not isinstance(value, dict):
                return
            kind = value.get("kind")
            if kind == "none":
                self.activities = list(value.get("activities") or [])
                self._render_activities()
                self.set_status("没有可签到活动", "info")
                self._show_message("一键签到", "当前课程没有进行中且未签到的点名活动")
                return
            if kind == "multiple":
                self.activities = list(value.get("activities") or [])
                self._render_activities()
                count = len(
                    signable_attendance_activities(
                        self.activities,
                        allow_ended=True,
                        signed_checker=(
                            session.attendance_api.is_signed
                            if session.attendance_api is not None
                            else None
                        ),
                    )
                )
                self.set_status(f"找到 {count} 个可签到点名", "info")
                self._show_message(
                    "一键签到", f"找到 {count} 个可签到点名，请手动选择目标"
                )
                return
            self._sign_done(value.get("result"), None)

        self.controller.run_async(operation, done)

    # ------------------------------------------------------------------
    # 工具页动作
    # ------------------------------------------------------------------

    def query_diagnostic(self) -> None:
        course = self._current_course()
        if course is None or self.controller.session.activities_api is None:
            self._show_message("诊断", "请先刷新并选择课程")
            return
        self.set_status("正在生成诊断...", "info")

        def operation() -> Any:
            api = self.controller.session.activities_api
            activities = api.list_activities(course)
            return _diagnostic(course, activities, base_url=api.lms_base_url)

        def done(value: Any, error: Exception | None) -> None:
            if error is not None:
                self._handle_error("诊断", error)
                return
            self.write_log("诊断结果", value)
            self.set_status("诊断已完成，详见日志", "success")
            self.switch_page("log")

        self.controller.run_async(operation, done)

    def _selected_selection(self) -> tuple[Course | None, dict[str, Any] | None]:
        if (
            not self.selected_activity
            or _as_int(self.selected_activity.get("relationType")) != 4
        ):
            return None, None
        return self._current_course(), self.selected_activity

    def query_selection_evidence(self) -> None:
        course, activity = self._selected_selection()
        if course is None or activity is None or self.controller.session.activities_api is None:
            self._show_message("选人详情", "请先查询选人，再选中一个选人活动")
            return
        self.set_status("正在读取选人详情...", "info")

        def operation() -> Any:
            return build_selection_evidence(
                course,
                activity,
                activity_service=self.controller.session.activities_api,
            )

        def done(value: Any, error: Exception | None) -> None:
            if error is not None:
                self._handle_error("选人详情", error)
                return
            self.write_log("选人详情", value)
            self.set_status("选人详情已完成，详见日志", "success")
            self.switch_page("log")

        self.controller.run_async(operation, done)

    def _snapshot_path(self) -> Path:
        return self.controller.session.app_dir / "ulearning_selection_snapshot.json"

    def record_selection_snapshot(self) -> None:
        course, activity = self._selected_selection()
        if course is None or activity is None or self.controller.session.activities_api is None:
            self._show_message("记录选人快照", "请先选中一个选人活动")
            return

        def operation() -> dict[str, Any]:
            evidence = build_selection_evidence(
                course,
                activity,
                activity_service=self.controller.session.activities_api,
            )
            record = {
                "activity_id": str(
                    activity.get("relationId", activity.get("id", ""))
                ),
                "snapshot_kind": "baseline",
                "captured_at": datetime.now().isoformat(timespec="seconds"),
                "baseline": evidence,
            }
            self._snapshot_path().write_text(
                json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            return record

        def done(value: Any, error: Exception | None) -> None:
            if error is not None:
                self._handle_error("记录选人快照", error)
                return
            self.selection_snapshot = (
                value if isinstance(value, dict) else None
            )
            self.write_log("选人快照已保存", value)
            self.set_status("选人快照已保存", "success")

        self.controller.run_async(operation, done)

    def _load_snapshot(self) -> dict[str, Any] | None:
        if self.selection_snapshot is not None:
            return self.selection_snapshot
        try:
            data = json.loads(self._snapshot_path().read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        if isinstance(data, dict):
            self.selection_snapshot = data
            return data
        return None

    def compare_selection_snapshot(self) -> None:
        course, activity = self._selected_selection()
        if course is None or activity is None or self.controller.session.activities_api is None:
            self._show_message("对比选人快照", "请先选中一个选人活动")
            return
        snapshot = self._load_snapshot()
        if snapshot is None:
            self._show_message("对比选人快照", "还没有基准快照，请先记录选人快照")
            return
        activity_id = str(activity.get("relationId", activity.get("id", "")))
        if str(snapshot.get("activity_id", "")) != activity_id:
            self._show_message("对比选人快照", "基准快照与当前活动不匹配")
            return

        def operation() -> dict[str, Any]:
            after = build_selection_evidence(
                course,
                activity,
                activity_service=self.controller.session.activities_api,
            )
            before = snapshot.get("baseline")
            return {
                "activity_id": activity_id,
                "before_captured_at": snapshot.get("captured_at"),
                "after_captured_at": after.get("captured_at"),
                "student_activity_changes": _diff_json(
                    (before or {}).get("student_activity"), after.get("student_activity")
                ),
                "classroom_match_changes": _diff_json(
                    (before or {}).get("classroom_matches"), after.get("classroom_matches")
                ),
            }

        def done(value: Any, error: Exception | None) -> None:
            if error is not None:
                self._handle_error("对比选人快照", error)
                return
            self.write_log("选人快照对比", value)
            self.set_status("快照对比已完成，详见日志", "success")
            self.switch_page("log")

        self.controller.run_async(operation, done)

    def open_official_page(self) -> None:
        try:
            webbrowser.open(OFFICIAL_PAGE)
            self.write_log("已请求打开优学院官方页面", {"url": OFFICIAL_PAGE})
            self.set_status("已请求打开官方页面", "info")
        except Exception as exc:  # noqa: BLE001
            self._handle_error("打开官方页面", exc)


# ---------------------------------------------------------------------------
# 应用入口
# ---------------------------------------------------------------------------


class MobileApp(App):
    title = "签到器"

    def build(self):
        self.session = MobileSession(Path(self.user_data_dir))
        self.manager = ScreenManager(transition=SlideTransition(duration=0.18))
        self.login_screen = LoginScreen(self, name="login")
        self.main_screen = MainScreen(self, name="main")
        self.manager.add_widget(self.login_screen)
        self.manager.add_widget(self.main_screen)
        if self.session.restore():
            self.manager.current = "main"
        else:
            self.manager.current = "login"
        return self.manager

    def show_main(self) -> None:
        self.manager.current = "main"

    def logout(self, message: str = "已退出登录") -> None:
        self.session.clear_session()
        self.main_screen.reset_state()
        self.login_screen.set_message(message)
        self.manager.current = "login"

    def run_async(
        self,
        operation: Callable[[], Any],
        callback: Callable[[Any, Exception | None], None],
    ) -> None:
        """后台执行，然后在主线程回调。

        注意：不能把 `except ... as exc` 的 exc 直接塞进延迟执行的 lambda——
        Python 会在 except 块结束时删除该名字，lambda 真正运行时读到的是已
        被清空的闭包单元，会抛 NameError，导致错误回调整个失效、界面卡在
        "正在... " 状态不动。这里先把它转存到独立变量再交给回调。
        """

        def worker() -> None:
            result: Any = None
            error: Exception | None = None
            try:
                result = operation()
            except Exception as exc:  # noqa: BLE001
                error = exc
            Clock.schedule_once(lambda _dt: callback(result, error), 0)

        threading.Thread(target=worker, daemon=True).start()


def _configure_desktop() -> None:
    """桌面调试时的窗口尺寸（Android 上自动跳过）。"""
    try:
        from kivy.utils import platform as kivy_platform

        if kivy_platform in ("win", "macosx", "linux"):
            from kivy.core.window import Window

            Window.size = (420, 880)
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    _configure_desktop()
    MobileApp().run()
