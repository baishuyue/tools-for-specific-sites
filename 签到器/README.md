# 签到器（优学院签到助手）

面向特定站点课程、活动查询与签到工具。使用优学院**学生端官方接口**完成查询和正常签到，支持命令行（CLI）、Tkinter 图形界面与 Android App 三种使用形态。

## 版本演进

本项目从一个"黑框"控制台脚本起步，逐步迭代为支持定位签到的多端工具，各版本源码完整保留在 [`源码/`](./源码/) 目录：

| 版本 | 日期 | 形态 | 主要内容 |
|---|---|---|---|
| v0.1 黑框控制台版 | 2026-09 上旬 | 源码 + Windows exe | 单文件脚本（`ulearning_standalone.py`，1874 行）：课程/活动查询、手动提交一次签到；CLI + Tkinter 双模式；配套 `run_ulearning.bat` 一键启动 |
| v0.2 中期迭代版 | 2026-09 中旬 | 源码 | 接口清单系统梳理、补签请求分析、诊断能力增强（`test3.py`，2998 行） |
| v1.1.0 工程化版 | 2026-09 下旬 | 源码 + 首个安卓 APK | 多坐标系（`gcj02`/`wgs84`/`bd09`）定位签到、`--dry-run` 演练与本地自检、Kivy 安卓壳（`android_app.py`）、Buildozer 打包、GitHub Actions 在线构建 APK |
| v1.2.1 当前版 | 2026-10 | 源码 + Windows + Android | 定位签到完善、新增 [`一键签到.py`](./源码/v1.2.1-当前版/一键签到.py)（自动扫描 + 监听模式）、安卓 UI 全面重制（卡片式布局、内置中文字体、真实 GPS 定位读取） |

> 坐标系口径：v1.1.0 及更早版本统一转换为 GCJ02 提交；v1.2.1 起确认优学院按**百度坐标系（BD09）**判定，程序改为统一转换为 BD09 提交，默认坐标系同步改为 `bd09`。

各版本详细说明见对应目录：

```
源码/
├── v0.1-黑框控制台版/    # ulearning_standalone.py + run_ulearning.bat
├── v0.2-中期迭代版/      # test3.py（中期形态）
├── v1.1.0-工程化版/      # 完整工程：桌面版 + 安卓壳 + CI 工作流 + 字体资源
└── v1.2.1-当前版/        # 当前桌面版 + 一键签到 + 重制安卓工程（android/）
```

## 快速开始

### 方式一：源码运行（推荐，功能最新）

环境要求：Windows / macOS / Linux，Python 3.10+。

```powershell
cd 源码/v1.2.1-当前版
python -m pip install -r requirements.txt
```

1. 打开 `ulearning-cmd.json`，在 `users` 中填写自己的登录名和密码（或从浏览器复制 `auth` 令牌与 `userID`）；
2. 运行：

```powershell
# 不带子命令 → 打开图形界面
python test3.py

# 本地自检（不访问服务器）
python test3.py self-test

# 查询课程 / 查询进行中的活动
python test3.py list-courses
python test3.py check --active-only --json

# 预览签到请求（不提交）
python test3.py signin --course-id <课程ID> --activity-id <活动ID> `
  --location "113.879022,22.901269" --coord-system bd09 --dry-run

# 正式提交一次签到
python test3.py signin --course-id <课程ID> --activity-id <活动ID> `
  --location "113.879022,22.901269" --coord-system bd09 --confirm
```

#### 一键签到（v1.2.1 新增）

纯 PC 接口版，无需模拟器与定位软件，自动处理所有"进行中且未签"的点名：

```powershell
python 一键签到.py                 # 扫描所有课程，自动签掉所有进行中的定位签到
python 一键签到.py --code 123456   # 同时处理数字码/二维码签到
python 一键签到.py --watch         # 持续监听模式，每 20 秒自动检查
python 一键签到.py --dry-run       # 只演练，不真正提交
```

首次运行会交互式引导输入账号并保存到同目录 `ulearning-cmd.json`。

### 方式二：Windows 版

[`Windows版/优学院签到助手-v0.1.exe`](./Windows版/) 为 v0.1 时期的打包版（黑框控制台形态）。

- 双击运行即可；如需配置账号，在 exe 同目录放置填写好的 `ulearning-cmd.json`；
- 功能较旧，仅包含查询与手动签到，建议优先使用 v1.2.1 源码版。

### 方式三：Android 版

[`安卓版/签到器-1.2.1-debug.apk`](./安卓版/) 为当前最新安卓版（v1.2.1）。

1. 下载 APK 并安装（需允许安装未知来源应用）；
2. 打开 App，使用学校优学院账号登录，可勾选"记住本次登录"（令牌保存在应用私有目录）；
3. 功能覆盖：课程下拉选择、活动列表、位置输入（坐标系选择 / 读取手机真实 GPS 定位）、单条签到、对选中签到、一键签到、选人查询、诊断、日志查看；
4. 令牌失效时会自动回到登录页；"切换用户"清除当前会话。

> 注意：APK 为 debug 签名，仅用于个人使用，请勿分发含本人登录信息的安装包。

## 定位签到与坐标系说明

- 位置格式为 `经度,纬度`（注意顺序）；
- 优学院使用**百度坐标系（BD09）**：接口返回的教师中心点与签到提交均按 BD09 处理；百度地图输出的坐标可直接使用（`bd09`），系统 GPS 输出为 `wgs84`，高德/腾讯地图为 `gcj02`，程序会自动把所选坐标系的坐标统一转换为 BD09 后提交，并保留转换诊断；
- 服务器会自行校验教师位置、签到半径、课堂 ID 与活动状态。出现"距离老师太远"时，应优先检查真实 GPS 精度、坐标系选择与课堂 ID，而不能通过客户端绕过服务端距离校验。

## 从源码构建 Android APK

Windows 本机无法直接运行 Buildozer，推荐使用 GitHub Actions 在线构建（工程已内置工作流）：

1. 将 `源码/v1.2.1-当前版/android/` 推送到任意 GitHub 仓库；
2. 打开仓库 Actions 页面，工作流 `Build Qiandaoqi Android APK` 会在推送时自动触发（也可手动运行）；
3. 构建完成后在运行页面底部下载 Artifact `ulearning-dgut-debug-apk`；
4. 首次构建约 20~40 分钟（需下载 Android SDK/NDK），命中缓存后约 8~15 分钟。

Linux 环境亦可本地构建：

```bash
python3 -m pip install buildozer cython==0.29.37
buildozer -v android debug
```

## 常见问题

**`Read timed out (read timeout=10.0)`**：连接建立后 10 秒内未读到完整响应，多为校园网络或服务端繁忙，不等于签到失败。可将配置中 `timeout` 提高到 30 秒；写入请求不会自动重试以避免重复提交，超时后先用 `check` 查询服务器状态再决定是否重试。

**提示"距离老师太远"或"定位不准确"**：确认位置是当前教室附近的真实坐标、输入顺序为"经度,纬度"、坐标系选择与坐标来源一致
## 免责声明

仅在本人账号和获得授权的课程环境中使用，遵守学校规章、平台服务条款和适用法律。项目作者不对未经授权的使用、账号风险或课程记录后果负责。

## 作者

baishuyue （MIT License）
