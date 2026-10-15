# 签到器（优学院 DGUT · Android 版）

`test3.py` 核心逻辑 + Kivy 重制 UI 的安卓打包工程。

## 结构

```
main.py                       移动端 UI（登录 / 课程 / 活动 / 签到 / 工具 / 日志）
test3.py                      核心服务层（与桌面版同一文件，MD5 一致，未改动）
fonts/NotoSansSC-Regular.ttf  思源黑体常规（由可变字体实例化的静态字重）
fonts/NotoSansSC-Bold.ttf     思源黑体粗体
icon.png                      应用图标（沿用原 APK）
presplash.jpg                 启动画面（沿用原 APK）
buildozer.spec                打包配置（Python 3.11 / Kivy 2.3.0 / 双 ABI）
.github/workflows/            GitHub Actions 在线打包工作流
push-and-build.bat            一键推送并触发构建（Windows）
```

## 与原 APK 的关系

原 APK（`ulearningdgut-1.1.0-debug`）是 `test3.py` + `android_app.py`（Kivy 壳）的 Buildozer 产物。
本工程沿用同一打包路线，但：

- **UI 全面重制**：卡片式布局、圆角按钮、彩色状态徽章、底部导航，替代原版平铺控件
- **字体换成静态字重**：原 APK 内嵌的 `NotoSansSC-VF.ttf` 是可变形字重，部分 SDL2_ttf 版本渲染时会崩溃；
  已用 fonttools 实例化为 Regular/Bold 静态文件，兼容性更稳
- **签到逻辑零改动**：`test3.py` 与原桌面版 MD5 完全相同

## 在线打包（推荐，本机无需 Java）

### 方式一：一键脚本（Windows）

1. 在 GitHub 网页端新建仓库（建议私有），例如 `ulearning-dgut`
2. 用记事本打开 `push-and-build.bat`，把 `REPO_URL` 改成你的仓库地址
3. 双击运行，按提示完成推送
4. 打开仓库 → Actions → 下载 Artifact

### 方式二：手动命令

```bash
git init && git add -A
git commit -m "签到器 Android 版"
git branch -M main
git remote add origin https://github.com/<你的用户名>/<仓库名>.git
git push -u origin main
```

推送后在 GitHub → Actions 页面：

- 工作流名称：`Build Qiandaoqi Android APK`
- 产物 Artifact：`ulearning-dgut-debug-apk`
- 首次构建约 20~40 分钟（下载 Android SDK/NDK），命中缓存后约 8~15 分钟

命令行下载（需 gh CLI）：

```bash
gh run watch && gh run download --name ulearning-dgut-debug-apk
```

## 桌面调试

```bash
pip install kivy==2.3.1 requests
python main.py
```

窗口尺寸自动设为手机比例（420×880），会话与配置存放在系统应用数据目录。

> 注意：若在 Python 3.13 上运行桌面调试时出现字体渲染崩溃（SDL2_ttf 原生库缺陷），
> 请改用 Python 3.11 / 3.12 —— 这也是打包 APK 使用的版本。

## 首次使用

- 安装后在应用内登录（账号 = 学校优学院账号）
- 勾选「记住本次登录」后令牌保存在应用私有目录，下次自动登录
- 配置文件（ulearning-cmd.json）在应用私有目录自动生成，可按需修改默认坐标

## 免责声明

仅在本人账号和获得授权的课程环境中使用，遵守学校规章、平台服务条款和适用法律。
