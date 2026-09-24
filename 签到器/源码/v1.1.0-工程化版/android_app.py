"""Kivy Android client for the Ulearning attendance helper.

This module is deliberately separate from the desktop Tkinter UI.  It reuses
the tested HTTP/service classes from ``test3.py`` and keeps session metadata in
Kivy's private application directory instead of the public Downloads folder.
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

from test3 import (
    ActivityService,
    AppConfig,
    AttendanceService,
    AuthenticationError,
    Course,
    CourseService,
    HttpClient,
    LoginService,
    LoginResult,
    OFFICIAL_PAGE,
    RELATION_TYPE,
    _activity_times,
    _as_int,
    _diagnostic,
    _diff_json,
    _location_field_names,
    _location_from_mapping,
    _prepare_location,
    _resolve_config_path,
    activity_is_active,
    activity_status,
    build_selection_evidence,
    load_config,
    selection_state,
    signable_attendance_activities,
)

try:  # Keep the service layer importable for static checks on desktop CI.
    from kivy.app import App
    from kivy.clock import Clock
    from kivy.core.text import LabelBase
    from kivy.metrics import dp
    from kivy.uix.boxlayout import BoxLayout
    from kivy.uix.button import Button
    from kivy.uix.checkbox import CheckBox
    from kivy.uix.dropdown import DropDown
    from kivy.uix.gridlayout import GridLayout
    from kivy.uix.label import Label
    from kivy.uix.popup import Popup
    from kivy.uix.screenmanager import Screen, ScreenManager
    from kivy.uix.scrollview import ScrollView
    from kivy.uix.spinner import Spinner, SpinnerOption
    from kivy.uix.tabbedpanel import TabbedPanel, TabbedPanelItem
    from kivy.uix.textinput import TextInput

    KIVY_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without Kivy installed.
    KIVY_AVAILABLE = False

try:  # Optional on desktop; available in the Android build through Plyer.
    from plyer import gps as _plyer_gps
except ImportError:  # pragma: no cover - desktop fallback.
    _plyer_gps = None

try:  # Android runtime permission bridge; unavailable during desktop tests.
    from android.permissions import Permission, request_permissions
except ImportError:  # pragma: no cover - desktop fallback.
    Permission = None
    request_permissions = None


class MobileSession:
    """Manage one student session and the services used by the mobile UI."""

    def __init__(self, app_dir: Path) -> None:
        self.app_dir = app_dir
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
        """Use the bundled template when the app is first launched."""
        bundled = Path(__file__).with_name("ulearning-cmd.json")
        if not self.config_path.exists() and bundled.exists():
            self.app_dir.mkdir(parents=True, exist_ok=True)
            self.config_path.write_text(bundled.read_text(encoding="utf-8"), encoding="utf-8")
        return load_config(self.config_path)

    def _load_saved_session(self) -> None:
        try:
            saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return
        if not isinstance(saved, dict):
            return
        self.username = str(saved.get("username", "")).strip()
        self.token = str(saved.get("token", "")).strip()
        self.user_id = int(saved.get("user_id", 0) or 0)
        self.role_id = int(saved.get("role_id", 0) or 0)

    @property
    def logged_in(self) -> bool:
        return bool(self.token and self.user_id)

    def _save_session(self) -> None:
        self.app_dir.mkdir(parents=True, exist_ok=True)
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

    def login(self, username: str, password: str, remember: bool = True) -> LoginResult:
        result = LoginService(
            self.config.application_base_url,
            timeout=self.config.timeout,
        ).login(username, password)
        self.username = username.strip()
        self.token = result.token
        self.user_id = result.user_id
        self.role_id = result.role_id
        self._build_services()
        if remember:
            self._save_session()
        else:
            self._delete_saved_session()
        return result

    def clear_session(self) -> None:
        self.token = ""
        self.username = ""
        self.user_id = 0
        self.role_id = 0
        self.client = None
        self.courses_api = None
        self.activities_api = None
        self.attendance_api = None
        self._delete_saved_session()

    def _delete_saved_session(self) -> None:
        try:
            self.state_path.unlink(missing_ok=True)
        except OSError:
            pass


if KIVY_AVAILABLE:

    FONT_PATH = Path(__file__).with_name("NotoSansSC-VF.ttf")
    if FONT_PATH.exists():
        LabelBase.register(name="NotoSansSC", fn_regular=str(FONT_PATH))
    FONT_NAME = "NotoSansSC" if FONT_PATH.exists() else "Roboto"

    class ChineseSpinnerOption(SpinnerOption):
        """Ensure dynamically created dropdown rows use the bundled CJK font."""

        def __init__(self, **kwargs: Any) -> None:
            kwargs.setdefault("font_name", FONT_NAME)
            kwargs.setdefault("font_size", dp(16))
            kwargs.setdefault("height", dp(52))
            kwargs.setdefault("size_hint_y", None)
            super().__init__(**kwargs)

    def mobile_spinner(**kwargs: Any) -> Spinner:
        kwargs.setdefault("font_name", FONT_NAME)
        kwargs.setdefault("option_cls", ChineseSpinnerOption)
        return Spinner(**kwargs)

    class LoginScreen(Screen):
        def __init__(self, controller: "MobileApp", **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.controller = controller
            root = BoxLayout(orientation="vertical", padding=dp(24), spacing=dp(14))
            root.add_widget(Label(text="优学院签到", font_name=FONT_NAME, font_size=dp(29), bold=True, size_hint_y=None, height=dp(58)))
            root.add_widget(Label(text="使用学校账号登录", font_name=FONT_NAME, color=(0.42, 0.46, 0.54, 1), size_hint_y=None, height=dp(28)))
            self.username = TextInput(hint_text="账号", multiline=False, font_name=FONT_NAME, size_hint_y=None, height=dp(50))
            self.password = TextInput(hint_text="密码", password=True, multiline=False, font_name=FONT_NAME, size_hint_y=None, height=dp(50))
            root.add_widget(self.username)
            root.add_widget(self.password)
            remember_row = BoxLayout(size_hint_y=None, height=dp(42), spacing=dp(8))
            self.remember = CheckBox(active=True, size_hint=(None, None), size=(dp(32), dp(32)))
            remember_row.add_widget(self.remember)
            remember_row.add_widget(Label(text="记住本次登录，下次自动登录", font_name=FONT_NAME, halign="left"))
            root.add_widget(remember_row)
            self.login_button = Button(text="登录", font_name=FONT_NAME, background_normal="", background_color=(0.12, 0.42, 0.78, 1), size_hint_y=None, height=dp(52))
            self.login_button.bind(on_release=self.submit)
            root.add_widget(self.login_button)
            self.message = Label(text="", font_name=FONT_NAME, color=(0.75, 0.2, 0.2, 1), halign="left")
            root.add_widget(self.message)
            root.add_widget(Label(font_name=FONT_NAME, text="学生账号仅能执行本人签到，定位签到需在规定范围内完成。", color=(0.45, 0.49, 0.56, 1), halign="left", valign="top"))
            self.add_widget(root)

        def submit(self, *_: Any) -> None:
            username = self.username.text.strip()
            password = self.password.text
            if not username or not password:
                self.message.text = "请输入账号和密码"
                return
            self.login_button.disabled = True
            self.message.text = "正在登录..."
            self.controller.run_async(
                lambda: self.controller.session.login(username, password, self.remember.active),
                self._success,
            )

        def _success(self, result: Any, error: Exception | None) -> None:
            self.login_button.disabled = False
            if error:
                self.message.text = str(error)
                return
            self.password.text = ""
            self.message.text = ""
            self.controller.show_main()


    class MainScreen(Screen):
        BUTTON_BLUE = (0.08, 0.42, 0.76, 1)
        BUTTON_TEAL = (0.08, 0.55, 0.50, 1)
        BUTTON_GRAY = (0.30, 0.36, 0.44, 1)

        def __init__(self, controller: "MobileApp", **kwargs: Any) -> None:
            super().__init__(name="main", **kwargs)
            self.controller = controller
            self.courses: list[Course] = []
            self.activities: list[dict[str, Any]] = []
            self.selected_activity: dict[str, Any] | None = None
            self.selection_snapshot: dict[str, Any] | None = None
            self.active_only = False
            self._gps_started = False
            self._last_gps: dict[str, Any] | None = None

            root = BoxLayout(orientation="vertical", padding=dp(10), spacing=dp(7))
            header = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
            self.user_label = Label(
                text="未登录", font_name=FONT_NAME, font_size=dp(15), halign="left", valign="middle"
            )
            self.user_label.bind(size=lambda widget, size: setattr(widget, "text_size", size))
            header.add_widget(self.user_label)
            switch = self._button("切换用户", self.BUTTON_GRAY, width=dp(94))
            switch.bind(on_release=lambda *_: controller.logout())
            header.add_widget(switch)
            root.add_widget(header)

            self.course_spinner = mobile_spinner(
                text="选择课程", values=(), size_hint_y=None, height=dp(50)
            )
            self.course_spinner.bind(text=self.course_changed)
            root.add_widget(self.course_spinner)

            self.status = Label(
                text="请选择课程",
                font_name=FONT_NAME,
                font_size=dp(14),
                color=(0.20, 0.56, 0.90, 1),
                halign="left",
                valign="middle",
                size_hint_y=None,
                height=dp(30),
            )
            self.status.bind(size=lambda widget, size: setattr(widget, "text_size", size))
            root.add_widget(self.status)

            self.tabs = TabbedPanel(do_default_tab=False, tab_width=dp(104))
            self.activity_tab = TabbedPanelItem(text="活动", font_name=FONT_NAME)
            self.tools_tab = TabbedPanelItem(text="工具", font_name=FONT_NAME)
            self.log_tab = TabbedPanelItem(text="日志", font_name=FONT_NAME)
            self._build_activity_tab()
            self._build_tools_tab()
            self._build_log_tab()
            self.tabs.add_widget(self.activity_tab)
            self.tabs.add_widget(self.tools_tab)
            self.tabs.add_widget(self.log_tab)
            self.tabs.switch_to(self.activity_tab)
            root.add_widget(self.tabs)
            self.add_widget(root)

        def _button(
            self,
            text: str,
            color: tuple[float, float, float, float] | None = None,
            *,
            width: float | None = None,
        ) -> Button:
            button = Button(
                text=text,
                font_name=FONT_NAME,
                font_size=dp(14),
                background_normal="",
                background_color=color or self.BUTTON_BLUE,
            )
            if width is not None:
                button.size_hint_x = None
                button.width = width
            return button

        def _build_activity_tab(self) -> None:
            panel = BoxLayout(orientation="vertical", padding=dp(7), spacing=dp(7))
            query_row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(7))
            all_button = self._button("查询全部活动")
            all_button.bind(on_release=lambda *_: self.query_activities("all"))
            selection_button = self._button("查询选人", self.BUTTON_TEAL)
            selection_button.bind(on_release=lambda *_: self.query_activities("selection"))
            query_row.add_widget(all_button)
            query_row.add_widget(selection_button)
            panel.add_widget(query_row)

            filter_row = BoxLayout(size_hint_y=None, height=dp(36), spacing=dp(4))
            self.active_check = CheckBox(active=False, size_hint_x=None, width=dp(38))
            self.active_check.bind(active=self._active_filter_changed)
            filter_row.add_widget(self.active_check)
            only_label = Label(text="只看进行中", font_name=FONT_NAME, halign="left")
            only_label.bind(size=lambda widget, size: setattr(widget, "text_size", size))
            filter_row.add_widget(only_label)
            self.selected_label = Label(
                text="未选择活动", font_name=FONT_NAME, font_size=dp(13), halign="right"
            )
            self.selected_label.bind(size=lambda widget, size: setattr(widget, "text_size", size))
            filter_row.add_widget(self.selected_label)
            panel.add_widget(filter_row)

            scroll = ScrollView()
            self.activity_box = BoxLayout(
                orientation="vertical", spacing=dp(6), padding=(0, dp(2)), size_hint_y=None
            )
            self.activity_box.bind(minimum_height=self.activity_box.setter("height"))
            scroll.add_widget(self.activity_box)
            panel.add_widget(scroll)

            sign_row = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(7))
            selected_sign = self._button("对选中点名签到")
            selected_sign.bind(on_release=lambda *_: self.sign_selected())
            auto_sign = self._button("一键签到当前课程", self.BUTTON_TEAL)
            auto_sign.bind(on_release=lambda *_: self.auto_sign_current_course())
            sign_row.add_widget(selected_sign)
            sign_row.add_widget(auto_sign)
            panel.add_widget(sign_row)
            self.activity_tab.content = panel

        def _build_tools_tab(self) -> None:
            scroll = ScrollView()
            panel = BoxLayout(
                orientation="vertical", padding=dp(8), spacing=dp(8), size_hint_y=None
            )
            panel.bind(minimum_height=panel.setter("height"))

            panel.add_widget(self._field_label("签到位置（经度,纬度）"))
            location_row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(6))
            self.location = TextInput(
                hint_text="113.879022,22.901269",
                multiline=False,
                font_name=FONT_NAME,
                font_size=dp(16),
            )
            self.coord = mobile_spinner(
                text="gcj02",
                values=("gcj02", "wgs84", "bd09"),
                size_hint_x=None,
                width=dp(96),
            )
            location_row.add_widget(self.location)
            location_row.add_widget(self.coord)
            panel.add_widget(location_row)

            panel.add_widget(self._field_label("地址（可选）"))
            self.address = TextInput(
                hint_text="例如：教学楼 A101", multiline=False, font_name=FONT_NAME, size_hint_y=None, height=dp(48)
            )
            panel.add_widget(self.address)
            panel.add_widget(self._field_label("签到码（教师提供时填写）"))
            self.attendance_code = TextInput(
                hint_text="签到码", multiline=False, font_name=FONT_NAME, size_hint_y=None, height=dp(48)
            )
            panel.add_widget(self.attendance_code)

            tools = GridLayout(cols=2, spacing=dp(7), size_hint_y=None, height=dp(192))
            for text, handler, color in (
                ("读取手机定位", self.read_device_location, self.BUTTON_TEAL),
                ("诊断", self.query_diagnostic, self.BUTTON_BLUE),
                ("选人详情", self.query_selection_evidence, self.BUTTON_TEAL),
                ("记录选人快照", self.record_selection_snapshot, self.BUTTON_GRAY),
                ("对比选人快照", self.compare_selection_snapshot, self.BUTTON_GRAY),
                ("刷新课程", self.refresh_courses, self.BUTTON_BLUE),
                ("打开官方页面", self.open_official_page, self.BUTTON_TEAL),
            ):
                button = self._button(text, color)
                button.bind(on_release=lambda _btn, action=handler: action())
                tools.add_widget(button)
            panel.add_widget(tools)
            help_label = Label(
                text="学生账号只能执行本人签到；定位签到仍由服务器校验距离。",
                font_name=FONT_NAME,
                font_size=dp(13),
                color=(0.55, 0.60, 0.68, 1),
                halign="left",
                valign="top",
                size_hint_y=None,
                height=dp(54),
            )
            help_label.bind(size=lambda widget, size: setattr(widget, "text_size", size))
            panel.add_widget(help_label)
            scroll.add_widget(panel)
            self.tools_tab.content = scroll

        def _build_log_tab(self) -> None:
            panel = BoxLayout(orientation="vertical", padding=dp(7), spacing=dp(7))
            toolbar = BoxLayout(size_hint_y=None, height=dp(42), spacing=dp(7))
            title = Label(text="请求与签到日志", font_name=FONT_NAME, halign="left")
            title.bind(size=lambda widget, size: setattr(widget, "text_size", size))
            clear = self._button("清空日志", self.BUTTON_GRAY, width=dp(94))
            clear.bind(on_release=lambda *_: self.clear_log())
            toolbar.add_widget(title)
            toolbar.add_widget(clear)
            panel.add_widget(toolbar)
            self.log_output = TextInput(
                text="",
                readonly=True,
                multiline=True,
                font_name=FONT_NAME,
                font_size=dp(13),
                background_color=(0.07, 0.08, 0.10, 1),
                foreground_color=(0.88, 0.91, 0.95, 1),
                cursor_blink=False,
            )
            panel.add_widget(self.log_output)
            self.log_tab.content = panel

        @staticmethod
        def _field_label(text: str) -> Label:
            label = Label(
                text=text,
                font_name=FONT_NAME,
                font_size=dp(13),
                halign="left",
                size_hint_y=None,
                height=dp(24),
            )
            label.bind(size=lambda widget, size: setattr(widget, "text_size", size))
            return label

        def on_pre_enter(self, *_: Any) -> None:
            session = self.controller.session
            self.user_label.text = f"当前用户：{session.username}\nID {session.user_id}"
            if not self.location.text:
                self.location.text = session.config.default_location
            if not self.address.text:
                self.address.text = session.config.default_address
            self.coord.text = session.config.default_coord_system
            self.write_log(f"已登录：{session.username} (ID {session.user_id}, role {session.role_id})")
            self.refresh_courses()

        def on_pre_leave(self, *_: Any) -> None:
            self.stop_device_location()

        def read_device_location(self) -> None:
            """Read one real Android location fix and copy it into the sign form."""
            if _plyer_gps is None:
                message = "当前构建未包含手机定位模块；请重新安装带 GPS 支持的 APK。"
                self.set_status(message, "error")
                self.write_log(message)
                self._show_message("手机定位", message)
                return
            if request_permissions is not None and Permission is not None:
                try:
                    request_permissions(
                        [Permission.ACCESS_FINE_LOCATION, Permission.ACCESS_COARSE_LOCATION]
                    )
                except Exception as exc:
                    self.write_log(f"请求定位权限失败：{exc}")
            try:
                _plyer_gps.configure(
                    on_location=self._on_gps_location,
                    on_status=self._on_gps_status,
                )
                _plyer_gps.start(minTime=1000, minDistance=1)
                self._gps_started = True
                self.set_status("正在读取手机 GPS，等待定位结果...")
                self.write_log("已启动手机 GPS；请打开系统定位并保持网络/GPS 可用")
            except Exception as exc:
                self._gps_started = False
                self.set_status(f"启动手机 GPS 失败：{exc}", "error")
                self.write_log(f"启动手机 GPS 失败：{exc}")
                self._show_message("手机定位", str(exc))

        def stop_device_location(self) -> None:
            if _plyer_gps is None or not self._gps_started:
                return
            try:
                _plyer_gps.stop()
            except Exception as exc:
                self.write_log(f"停止手机 GPS 失败：{exc}")
            finally:
                self._gps_started = False

        def _on_gps_status(self, status: str, message: str) -> None:
            Clock.schedule_once(
                lambda _dt: self.write_log(
                    f"GPS 状态：{status}" + (f" / {message}" if message else "")
                ),
                0,
            )

        def _on_gps_location(self, **kwargs: Any) -> None:
            Clock.schedule_once(lambda _dt: self._apply_gps_location(kwargs), 0)

        def _apply_gps_location(self, payload: Mapping[str, Any]) -> None:
            try:
                lat = float(payload.get("lat", payload.get("latitude")))
                lon = float(payload.get("lon", payload.get("lng", payload.get("longitude"))))
            except (TypeError, ValueError):
                self.write_log("GPS 返回了无法解析的坐标", dict(payload))
                return
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                self.write_log("GPS 返回了超出范围的坐标", dict(payload))
                return
            accuracy_value = payload.get("accuracy")
            try:
                accuracy = float(accuracy_value) if accuracy_value not in (None, "") else None
            except (TypeError, ValueError):
                accuracy = None
            self.location.text = f"{lon:.6f},{lat:.6f}"
            self.coord.text = "wgs84"
            self._last_gps = {
                "longitude": lon,
                "latitude": lat,
                "accuracy_m": accuracy,
                "provider": payload.get("provider", ""),
                "timestamp": datetime.now().isoformat(timespec="seconds"),
            }
            accuracy_text = f"，精度约 {accuracy:.1f} 米" if accuracy is not None else ""
            warning = "；精度较低，请在室内靠近窗户或开启高精度定位" if accuracy is not None and accuracy > 100 else ""
            self.set_status(f"已读取手机位置：{lon:.6f},{lat:.6f}{accuracy_text}", "success" if not warning else "info")
            self.write_log("已读取真实手机位置（按 WGS84 转换为发送坐标）", self._last_gps)
            if warning:
                self.write_log(warning)
            self.stop_device_location()

        def set_status(self, text: str, kind: str = "info") -> None:
            self.status.text = text
            self.status.color = {
                "success": (0.16, 0.72, 0.43, 1),
                "error": (0.92, 0.30, 0.28, 1),
            }.get(kind, (0.20, 0.56, 0.90, 1))

        def write_log(self, message: str, payload: Any | None = None) -> None:
            timestamp = datetime.now().strftime("%H:%M:%S")
            block = f"[{timestamp}] {message}\n"
            if payload is not None:
                block += json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n"
            text = (self.log_output.text + block)[-50000:]
            self.log_output.text = text
            self.log_output.cursor = (0, len(text.splitlines()))
            self.log_output.scroll_y = 0

        def clear_log(self) -> None:
            self.log_output.text = ""
            self.write_log("日志已清空")

        def _show_message(self, title: str, message: str) -> None:
            layout = BoxLayout(orientation="vertical", padding=dp(14), spacing=dp(10))
            label = Label(text=message, font_name=FONT_NAME, halign="left", valign="middle")
            label.bind(size=lambda widget, size: setattr(widget, "text_size", size))
            close = self._button("确定", self.BUTTON_BLUE)
            layout.add_widget(label)
            layout.add_widget(close)
            popup = Popup(
                title=title,
                title_font=FONT_NAME,
                content=layout,
                size_hint=(0.88, 0.42),
                auto_dismiss=False,
            )
            close.bind(on_release=popup.dismiss)
            popup.open()

        def _current_course(self) -> Course | None:
            values = list(self.course_spinner.values)
            if self.course_spinner.text not in values:
                return None
            index = values.index(self.course_spinner.text)
            return self.courses[index] if index < len(self.courses) else None

        def refresh_courses(self) -> None:
            self.set_status("正在加载课程...")
            self.write_log("开始加载课程")
            self.controller.run_async(
                self.controller.session.courses_api.list_courses, self._courses_loaded
            )

        def _courses_loaded(self, value: Any, error: Exception | None) -> None:
            if self._handle_error("加载课程", error):
                return
            self.courses = list(value or [])
            labels = tuple(f"{item.course_id} | {item.name}" for item in self.courses)
            self.course_spinner.values = labels
            self.set_status(f"已加载 {len(self.courses)} 门课程", "success")
            self.write_log(f"已加载 {len(self.courses)} 门课程")
            if labels and self.course_spinner.text not in labels:
                self.course_spinner.text = labels[0]

        def course_changed(self, _spinner: Any, text: str) -> None:
            if text in self.course_spinner.values:
                self.write_log(f"选择课程：{text}")
                self.query_activities("all")

        def _active_filter_changed(self, _widget: Any, active: bool) -> None:
            self.active_only = active
            self._render_activities(self.activities)
            self.write_log("只看进行中：" + ("开启" if active else "关闭"))

        def query_activities(self, mode: str = "all") -> None:
            course = self._current_course()
            service = self.controller.session.activities_api
            if course is None or service is None:
                self._show_message("提示", "请先选择课程")
                return
            label = "选人活动" if mode == "selection" else "全部活动"
            self.set_status(f"正在查询{label}...")
            self.write_log(f"开始查询{label}：{course.course_id} | {course.name}")
            self.controller.run_async(
                lambda: service.list_activities(course),
                lambda value, error: self._activities_loaded(value, error, mode),
            )

        def _activities_loaded(self, value: Any, error: Exception | None, mode: str) -> None:
            if self._handle_error("查询活动", error):
                return
            all_activities = list(value or [])
            if mode == "selection":
                all_activities = [item for item in all_activities if _as_int(item.get("relationType")) == 4]
            self.activities = all_activities
            shown = self._render_activities(all_activities)
            self.set_status(f"查询到 {shown} 个活动", "success")
            self.write_log(f"查询完成：显示 {shown} 个活动")

        def _render_activities(self, activities: list[dict[str, Any]]) -> int:
            self.activity_box.clear_widgets()
            self.selected_activity = None
            self.selected_label.text = "未选择活动"
            displayed = [
                item for item in activities if not self.active_only or activity_is_active(item)
            ]
            attendance_service = self.controller.session.attendance_api
            for activity in displayed:
                relation_type = _as_int(activity.get("relationType"), -1) or -1
                type_name = RELATION_TYPE.get(relation_type, f"未知({relation_type})")
                title = str(activity.get("title", activity.get("name", "未命名活动"))).replace("\n", " ")
                status = activity_status(activity)
                extra = ""
                signed = False
                if relation_type == 1 and attendance_service is not None:
                    signed = attendance_service.is_signed(activity)
                    extra = " | " + ("已签到" if signed else "未签到")
                elif relation_type in (3, 4):
                    extra = " | " + selection_state(activity)[0]
                row = BoxLayout(size_hint_y=None, height=dp(76), spacing=dp(6))
                detail = self._button(
                    f"{type_name} | {status}{extra}\n{title}", self.BUTTON_GRAY
                )
                detail.halign = "left"
                detail.valign = "middle"
                detail.bind(size=lambda widget, size: setattr(widget, "text_size", (size[0] - dp(12), size[1])))
                detail.bind(on_release=lambda _btn, item=activity: self.select_activity(item))
                row.add_widget(detail)
                action = self._button(
                    "已签到" if signed else ("签到" if relation_type == 1 else "选择"),
                    self.BUTTON_GRAY if signed else self.BUTTON_BLUE,
                    width=dp(78),
                )
                action.disabled = signed
                if relation_type == 1:
                    action.bind(on_release=lambda _btn, item=activity: self.sign(item))
                else:
                    action.bind(on_release=lambda _btn, item=activity: self.select_activity(item))
                row.add_widget(action)
                self.activity_box.add_widget(row)
            return len(displayed)

        def select_activity(self, activity: dict[str, Any]) -> None:
            self.selected_activity = activity
            activity_id = str(activity.get("relationId", activity.get("id", "")))
            self.selected_label.text = f"已选：{activity_id}"
            self.set_status(f"已选择活动 {activity_id}")
            self.write_log(
                f"选中活动：{activity_id} | {activity.get('title', activity.get('name', ''))}"
            )

        def _signin_options(self) -> dict[str, str]:
            return {
                "location": self.location.text.strip(),
                "address": self.address.text.strip(),
                "coord_system": self.coord.text.strip() or "gcj02",
                "attendance_code": self.attendance_code.text.strip(),
            }

        def sign_selected(self) -> None:
            if self.selected_activity is None:
                self._show_message("签到", "请先在活动列表中选择一个点名活动")
                return
            self.sign(self.selected_activity)

        def sign(self, activity: dict[str, Any]) -> None:
            course = self._current_course()
            service = self.controller.session.attendance_api
            if course is None or service is None:
                self._show_message("签到", "请先选择课程")
                return
            if _as_int(activity.get("relationType")) != 1:
                self._show_message("签到", "选中的活动不是点名签到活动")
                return
            if service.is_signed(activity):
                self._show_message("签到状态", "服务器状态显示已签到")
                return
            can_sign, reason = service.can_sign(activity, allow_ended=service.allow_ended)
            if not can_sign:
                self._show_message("签到状态", reason)
                return
            activity_id = str(activity.get("relationId", activity.get("id", "")))
            options = self._signin_options()
            client_location_diagnostic: dict[str, Any] | None = None
            if options["location"]:
                try:
                    _sent_location, client_location_diagnostic = _prepare_location(
                        options["location"], options["coord_system"]
                    )
                except Exception as exc:
                    self.write_log(f"客户端坐标预处理失败：{exc}")
            self.set_status("正在提交签到...")
            self.write_log(
                f"准备签到：活动 {activity_id}",
                {
                    "course_id": course.course_id,
                    "activity_id": activity_id,
                    "classroom_id": activity.get("classroomId", activity.get("_roomId", "")),
                    "activity_status": activity_status(activity),
                    "time_status": activity.get("timeStatus"),
                    **options,
                    "client_location_diagnostic": client_location_diagnostic,
                },
            )
            self.controller.run_async(
                lambda: service.sign(course, activity, **options, dry_run=False),
                self._sign_done,
            )

        def _sign_done(self, value: Any, error: Exception | None) -> None:
            if self._handle_error("签到", error, popup=True):
                return
            result = dict(value or {})
            success = bool(result.get("success"))
            message = str(result.get("message", "请求已完成"))
            self.set_status(("签到成功：" if success else "签到失败：") + message, "success" if success else "error")
            self.write_log("签到服务器响应", result)
            if not success and result.get("location_diagnostic"):
                self.write_log(
                    "服务端明确拒绝了定位条件；这不是客户端本地成功状态，需核对真实 GPS、课堂教师位置和活动配置",
                    result.get("location_diagnostic"),
                )
            self._show_message("签到成功" if success else "签到失败", message)
            self.tabs.switch_to(self.log_tab)
            course = self._current_course()
            service = self.controller.session.activities_api
            if course is not None and service is not None:
                self.controller.run_async(
                    lambda: service.list_activities(course),
                    lambda refreshed, refresh_error: self._refresh_after_sign(refreshed, refresh_error),
                )

        def _refresh_after_sign(self, value: Any, error: Exception | None) -> None:
            if error:
                self.write_log(f"签到后刷新状态失败：{error}")
                return
            self.activities = list(value or [])
            self._render_activities(self.activities)
            self.write_log("已重新查询服务器签到状态")

        def auto_sign_current_course(self) -> None:
            course = self._current_course()
            activity_service = self.controller.session.activities_api
            attendance_service = self.controller.session.attendance_api
            if course is None or activity_service is None or attendance_service is None:
                self._show_message("一键签到", "请先选择课程")
                return
            options = self._signin_options()
            self.set_status("正在查找可签到活动...")
            self.write_log(f"一键签到检查：{course.course_id} | {course.name}")

            def operation() -> dict[str, Any]:
                activities = activity_service.list_activities(course)
                candidates = signable_attendance_activities(
                    activities,
                    allow_ended=attendance_service.allow_ended,
                    signed_checker=attendance_service.is_signed,
                )
                if len(candidates) != 1:
                    return {"kind": "none" if not candidates else "multiple", "activities": candidates}
                result = attendance_service.sign(
                    course, candidates[0], **options, dry_run=False
                )
                return {"kind": "signed", "activities": candidates, "result": result}

            self.controller.run_async(operation, self._auto_sign_done)

        def _auto_sign_done(self, value: Any, error: Exception | None) -> None:
            if self._handle_error("一键签到", error, popup=True):
                return
            outcome = dict(value or {})
            candidates = list(outcome.get("activities", []))
            kind = outcome.get("kind")
            if kind == "none":
                self.set_status("当前课程没有可签到活动")
                self.write_log("当前课程没有进行中且未签到的点名活动")
                self._show_message("一键签到", "当前课程没有可签到活动")
            elif kind == "multiple":
                self.activities = candidates
                self._render_activities(candidates)
                self.tabs.switch_to(self.activity_tab)
                message = f"找到 {len(candidates)} 个可签到点名，请手动选择目标"
                self.set_status(message)
                self.write_log(message)
                self._show_message("一键签到", message)
            else:
                self._sign_done(outcome.get("result", {}), None)

        def query_diagnostic(self) -> None:
            course = self._current_course()
            service = self.controller.session.activities_api
            if course is None or service is None:
                self._show_message("诊断", "请先选择课程")
                return
            self.set_status("正在生成诊断...")
            self.controller.run_async(
                lambda: _diagnostic(
                    course, service.list_activities(course), base_url=service.lms_base_url
                ),
                lambda value, error: self._tool_result("诊断结果", value, error),
            )

        def _selected_selection(self) -> tuple[Course | None, dict[str, Any] | None]:
            activity = self.selected_activity
            if activity is None or _as_int(activity.get("relationType")) != 4:
                return None, None
            return self._current_course(), activity

        def query_selection_evidence(self) -> None:
            course, activity = self._selected_selection()
            service = self.controller.session.activities_api
            if course is None or activity is None or service is None:
                self._show_message("选人详情", "请先查询选人，再选中一个选人活动")
                return
            self.set_status("正在读取选人详情...")
            self.controller.run_async(
                lambda: build_selection_evidence(course, activity, activity_service=service),
                lambda value, error: self._tool_result("选人详情", value, error),
            )

        def _snapshot_path(self) -> Path:
            return self.controller.session.app_dir / "ulearning_selection_snapshot.json"

        def record_selection_snapshot(self) -> None:
            course, activity = self._selected_selection()
            service = self.controller.session.activities_api
            if course is None or activity is None or service is None:
                self._show_message("记录快照", "请先选中一个选人活动")
                return

            def operation() -> dict[str, Any]:
                evidence = build_selection_evidence(course, activity, activity_service=service)
                evidence["snapshot_kind"] = "baseline"
                self._snapshot_path().write_text(
                    json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
                return evidence

            self.controller.run_async(operation, self._snapshot_recorded)

        def _snapshot_recorded(self, value: Any, error: Exception | None) -> None:
            if self._handle_error("记录选人快照", error, popup=True):
                return
            self.selection_snapshot = dict(value or {})
            self.set_status("选人快照已保存", "success")
            self.write_log("选人快照已保存", self.selection_snapshot)
            self.tabs.switch_to(self.log_tab)

        def _load_snapshot(self) -> dict[str, Any] | None:
            if self.selection_snapshot is not None:
                return dict(self.selection_snapshot)
            try:
                value = json.loads(self._snapshot_path().read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                return None
            return dict(value) if isinstance(value, Mapping) else None

        def compare_selection_snapshot(self) -> None:
            course, activity = self._selected_selection()
            service = self.controller.session.activities_api
            before = self._load_snapshot()
            if course is None or activity is None or service is None:
                self._show_message("对比快照", "请先选中一个选人活动")
                return
            if before is None:
                self._show_message("对比快照", "还没有基准快照，请先记录选人快照")
                return
            activity_id = str(activity.get("relationId", activity.get("id", "")))
            if str(before.get("activity_id", "")) != activity_id:
                self._show_message("对比快照", "基准快照与当前活动不匹配")
                return

            def operation() -> dict[str, Any]:
                after = build_selection_evidence(course, activity, activity_service=service)
                before_matches = before.get("classroom_matches") or [{}]
                after_matches = after.get("classroom_matches") or [{}]
                return {
                    "activity_id": activity_id,
                    "before_captured_at": before.get("captured_at"),
                    "after_captured_at": after.get("captured_at"),
                    "student_activity_changes": _diff_json(
                        before.get("student_activity", {}), after.get("student_activity", {})
                    ),
                    "classroom_match_changes": _diff_json(before_matches[0], after_matches[0]),
                    "student_vs_classroom_after": after.get("student_vs_classroom_diff", []),
                }

            self.controller.run_async(
                operation,
                lambda value, error: self._tool_result("选人快照对比", value, error),
            )

        def _tool_result(self, title: str, value: Any, error: Exception | None) -> None:
            if self._handle_error(title, error, popup=True):
                return
            self.set_status(f"{title}已完成", "success")
            self.write_log(title, value)
            self.tabs.switch_to(self.log_tab)

        def open_official_page(self) -> None:
            try:
                webbrowser.open(OFFICIAL_PAGE)
                self.write_log("已请求打开优学院官方页面")
            except Exception as exc:
                self._handle_error("打开官方页面", exc, popup=True)

        def _handle_error(
            self, action: str, error: Exception | None, *, popup: bool = False
        ) -> bool:
            if error is None:
                return False
            if isinstance(error, AuthenticationError):
                self.write_log(f"{action}失败：登录已过期")
                self.controller.logout(message="登录已过期，请重新登录")
                return True
            message = f"{action}失败：{error}"
            self.set_status(message, "error")
            self.write_log(message)
            if popup:
                self._show_message("请求失败", str(error))
            return True


    class MobileApp(App):
        title = "优学院签到"

        def build(self) -> ScreenManager:
            self.session = MobileSession(Path(self.user_data_dir))
            self.manager = ScreenManager()
            self.login_screen = LoginScreen(self, name="login")
            self.main_screen = MainScreen(self)
            self.manager.add_widget(self.login_screen)
            self.manager.add_widget(self.main_screen)
            if self.session.restore():
                self.manager.current = "main"
            return self.manager

        def show_main(self) -> None:
            self.manager.current = "main"

        def logout(self, message: str = "已退出登录") -> None:
            self.session.clear_session()
            self.login_screen.message.text = message
            self.manager.current = "login"

        def run_async(self, operation: Callable[[], Any], callback: Callable[[Any, Exception | None], None]) -> None:
            def worker() -> None:
                try:
                    value, error = operation(), None
                except Exception as exc:  # UI must always be released on API errors.
                    value, error = None, exc
                Clock.schedule_once(lambda _dt: callback(value, error), 0)

            threading.Thread(target=worker, daemon=True).start()


else:

    class MobileApp:  # pragma: no cover - only a helpful desktop error path.
        def run(self) -> None:
            raise RuntimeError("Android UI requires Kivy. Install requirements-android.txt first.")


if __name__ == "__main__":
    MobileApp().run()
