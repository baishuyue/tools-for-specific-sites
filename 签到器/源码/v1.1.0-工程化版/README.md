# 优学院 DGUT 签到助手

这是一个面向东莞理工学院优学院账号的课程、活动查询与单次签到工具。项目使用优学院学生端接口完成查询和正常签到，支持命令行和 Tkinter 图形界面。

## 功能

- 查询课程、点名和其他课堂活动
- 查看活动是否进行中、是否已签到
- 使用学生端 `signByStu` 提交一次签到请求
- 支持位置签到的 `gcj02`、`wgs84`、`bd09` 坐标输入，并统一发送“经度,纬度”
- 支持 `--dry-run`、接口诊断和本地自检
- 对已结束活动只生成补签请求分析；真正修改记录必须使用服务端单独签发的教师或管理员令牌
- 请求失败时不会伪造本地成功状态

## 安全边界

本项目不包含、也不会实现以下行为：伪造身份令牌、把学生令牌提升为教师令牌、绕过服务端定位半径、直接读取或修改后端数据库。教师端补签只能使用学校服务端合法签发的教师/管理员令牌，并且应得到相应授权。

不要把密码、`AUTHORIZATION`、Cookie、日志、历史记录或诊断快照提交到公开仓库。本发布包中的 `ulearning-cmd.json` 只有空白模板字段，首次使用前请在本地填写。

## 环境

- Windows、macOS 或 Linux
- Python 3.10+
- 可访问学校优学院服务

安装依赖：

```powershell
python -m pip install -r requirements.txt
```

## 配置

复制并编辑 `ulearning-cmd.json`。至少填写学生账号的 `auth` 和 `userID`；也可以填写 `password`，由程序通过官方登录接口获取令牌。

```json
{
  "version": 2,
  "timeout": 30.0,
  "retries": 3,
  "backoff": 1.0,
  "activity_page_size": 100,
  "verify_delay": 1.5,
  "history_file": "ulearning_history.json",
  "default_location": "113.879022,22.901269",
  "default_coord_system": "gcj02",
  "default_address": "",
  "users": {
    "your-account": {
      "password": "",
      "auth": "",
      "userID": 0,
      "roleId": 9,
      "deviceInfo": " ulearning-cmd ",
      "terminalId": "0123456789abcdef"
    }
  },
  "activeUser": "your-account"
}
```

实际教室坐标应由你在现场用合法方式获取。服务器会自行判断教师位置和签到半径，固定示例坐标不保证可用。

也可用环境变量临时提供令牌，避免写入文件：

```powershell
$env:ULEARNING_AUTHORIZATION = "<学生端 AUTHORIZATION>"
```

## 常用命令

```powershell
# 本地检查，不访问服务
python test3.py self-test

# 查询课程
python test3.py list-courses

# 查询活动状态
python test3.py check --active-only --json
python test3.py list-activities --course-id <课程ID> --json

# 先预览请求，不提交服务器
python test3.py signin --course-id <课程ID> --activity-id <活动ID> `
  --location "113.879022,22.901269" --coord-system gcj02 --dry-run

# 提交一次正常学生签到
python test3.py signin --course-id <课程ID> --activity-id <活动ID> `
  --location "113.879022,22.901269" --coord-system gcj02 --confirm
```

不带子命令运行会打开图形界面：

```powershell
python test3.py
```

坐标说明：优学院服务端按**百度坐标系（BD09）**判定距离；GPS 常见输出是 `wgs84`，高德/腾讯地图为 `gcj02`，百度地图为 `bd09`。本版本（v1.1.0）当时统一转 GCJ02 提交，v1.2.1 起已改为统一转 BD09。程序会记录转换诊断，但不能替代真实的现场定位。

## 超时与定位失败

`Read timed out (read timeout=10.0)` 表示连接建立后 10 秒内没有读到完整响应，可能是校园网络、服务端繁忙或链路抖动，不等于签到业务失败。配置中的 `timeout` 可提高到 30 秒。写入请求不会自动重试，以避免重复提交；超时后先运行 `check` 查询服务器状态，再决定是否重试。

如果收到“距离老师太远”或“定位不准确”：

1. 确认位置是当前教室附近的真实坐标；
2. 确认输入顺序为 `经度,纬度`；
3. 根据坐标来源选择 `gcj02`、`wgs84` 或 `bd09`；
4. 仍被拒绝时只能联系老师调整签到状态，客户端不能合法绕过距离校验。

## 文件说明

- `test3.py`：主程序，支持 CLI 和 GUI。
- `ulearning-cmd.json`：本地配置模板，含敏感字段时不得提交到公开仓库。
- `requirements.txt`：Python 依赖。
- `.gitignore`：排除本地凭据、日志、历史和缓存。

## Android APK

`android_app.py` 是独立的手机界面，`main.py` 是 APK 入口。移动端包含：

- 首次打开显示登录页；可勾选“记住本次登录”；
- 记住后令牌保存在 Android 应用私有目录，下次启动自动恢复；
- 顶部显示当前账号和用户 ID；
- “切换用户”会清除当前会话并回到登录页；令牌失效时也会自动回到登录页；
- 课程下拉选择、活动列表、位置输入、坐标系选择和签到按钮均适配窄屏；
- “活动 / 工具 / 日志”页签包含全部活动、选人查询、选中签到、一键签到、诊断、选人详情和快照对比；
- 每次请求、签到参数、服务器响应与成功/失败结果都会显示在可滚动日志中；
- APK 内置 Noto Sans SC 字体，包括动态生成的课程下拉选项也使用该字体；
- “读取手机定位”会使用真实 Android GPS 获取一次定位，显示精度并填入 WGS84 坐标；
- “诊断”会记录活动的 classroom ID 与可能的定位字段；
- 网络请求在后台线程执行，不阻塞手机界面。

定位请求使用学生端 `signByStu`。客户端只做坐标顺序、WGS84/BD09 到 GCJ02 的正常转换和回读；服务器仍会校验教师位置、距离半径、课堂 ID 和活动状态。出现“距离老师太远”时，应优先检查真实 GPS 精度、坐标系、课堂 ID 与教师端位置设置；不应使用伪造位置、伪造令牌或绕过服务器距离校验。

在 Linux 或 GitHub Actions 中构建调试 APK：

```bash
python3 -m pip install buildozer cython==0.29.37
buildozer -v android debug
```

生成文件位于 `bin/`。集成到 RoboCon 仓库后，根目录的 `.github/workflows/ulearning-android.yml` 会自动构建；也可以在 GitHub 的 Actions 页面手动运行 `Build Ulearning Android APK`，再下载 `ulearning-dgut-debug-apk` artifact。Windows 本机不能直接完成 Buildozer 的 Android SDK/NDK 构建，因此 Windows 上的验证是 Python 编译和业务自检，APK 构建应以 Actions 的构建结果为准。

首次安装 APK 后请在应用内登录，不要把带真实 `session.json` 或账号令牌的 APK/压缩包公开分享。

## 免责声明

仅在本人账号和获得授权的课程环境中使用，遵守学校规章、平台服务条款和适用法律。项目作者不对未经授权的使用、账号风险或课程记录后果负责。
