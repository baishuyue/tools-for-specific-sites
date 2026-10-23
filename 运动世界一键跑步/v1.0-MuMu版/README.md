# 运动世界校园 · 一键跑步 v1.0（MuMu 版 · 历史归档）

> **本目录为旧版归档，仅供研究参考。** 新版已不再需要模拟器与 Root 环境，
> 直接使用单文件程序即可：[**v2.0 免安装版**](../v2.0-纯Windows版/)。

在 Windows 上一键完成当天校园跑的自动化工具：自动后台连接本机 MuMu 模拟器里已登录的「运动世界校园」，生成一条合规的 2km / 5 打卡点轨迹，完成加密、上传、提交，并自动轮询服务端核验结果。

- 环境已就绪时：点一下约 **3 秒**发完所有请求，提示「已提交、核验中」；
- 今天已经跑过：约 **2 秒**直接提示「今天已完成 + 有效里程」，不重复提交；
- 成绩由服务端核验（约 5–12 分钟），工具会自动轮询并在结果出来后弹窗「成功 / 失败」。

## 部署策略（架构说明）

工具采用「Windows 主控 + 安卓运行环境」两层结构：

| 层 | 职责 | 组成 |
|---|---|---|
| Windows 主控 | 轨迹生成、加密、对象存储上传、成绩签名、提交、结果轮询、GUI | 本工具（Python 3.11.9 + pycryptodome 4.1.0） |
| 安卓运行环境 | 保持账号登录态；为请求提供签名头 | MuMu 模拟器 6 + 运动世界校园 7.3.92 + Zygisk Next / LSPosed(Vector) / HMA / LocationSpoofer / SportSniffer |

**为什么要保留一个安卓环境**：客户端请求头依赖 App 内安全组件生成的签名，PC 端无法独立生成；且该 App 未提供账号密码登录（仅手机号一键登录 / 短信验证码）。因此保留一个已登录的 App（MuMu 后台静默运行）作为本地签名服务，是当前版本唯一稳定、可长期维护的方案。

**部署节奏**：
- **一次性部署**：按下方步骤在电脑上装配环境（约 40–60 分钟）；
- **日常使用**：双击 `一键跑步.bat` → 点「一键完成」→ 等待自动核验结果；
- **换账号**：在 MuMu 内切换到另一个运动世界账号，直接点「一键完成」即可（同校区，uid 自动读取）；
- **换校区**：打卡点 / 围栏不同，需准备该校区对应的轨迹与报文模板

## 版本锁定表

以下版本为本文档编写时（**2026-10-22**）核对官方发布信息后锁定的基准组合。升级任一组件前建议先在测试环境验证。

| 组件 | 锁定版本 | 获取渠道 | 许可 / 说明 |
|---|---|---|---|
| MuMu 模拟器 6（Windows） | **V6.3.0**（国内官网当前版本；6.x 系列均可，需含 `nx_main` 目录） | 官网 mumu.163.com | 网易免费闭源软件 |
| 运动世界校园 App | **7.3.92** | 各安卓应用商店 | 闭源商业应用 |
| Zygisk Next | **v1.5.2**（2026-10-13 发布） | github.com/LSPosed/ZygiskNext/releases | 自 v4-0.9.2 起**不再开源**，保留所有权利，禁止修改 / 再分发 |
| LSPosed / Vector | **v2.2**（2026-08-04 发布，JingMatrix 维护的 LSPosed 续作） | github.com/JingMatrix/Vector/releases | 开源（GPL-3.0）；原 LSPosed 最后版本为 v1.9.2（2023-10） |
| Hide My Applist（HMA） | **v3.8.3**（2026-08-01 发布） | github.com/Dr-TSNG/Hide-My-Applist/releases | 开源，许可见其仓库声明 |
| LocationSpoofer | **2.6.13**（**锁定，勿升 3.x**——新版需登录） | 按原作者发布渠道获取 | 第三方闭源应用 |
| SportSniffer 签名模块 | 包名 `com.doubao.sportsniff`，以模块发布版本为准 | 第三方 LSPosed 模块，**本仓库不包含** | 遵循模块作者的许可与说明 |
| Python（Windows 64 位） | **3.11.9**（3.10+ 均可，安装时勾选 **Add to PATH**，需含 Tcl/Tk） | python.org | PSF 许可 |
| pycryptodome | **4.1.0** | PyPI | BSD 类许可 |

## 部署步骤（一次性，约 40–60 分钟）

> 前置条件：Windows 10 / 11 64 位、内存 16GB 以上（8GB 可运行但较慢）、预留 10GB 磁盘。

1. **安装 MuMu 模拟器 6 V6.3.0**（建议装到非系统盘）→ 新建 **Android 15** 实例 → 在模拟器设置中开启 **KernelSU Root** 与 **ADB 调试**（记住实例 adb 端口，默认 `127.0.0.1:16384`）。
2. **安装运动世界校园 7.3.92**：在实例内通过应用商店安装，使用本人手机号 + 短信验证码**登录一次**（之后长期免登）。
3. **刷入 Zygisk Next v1.5.2**：在 KernelSU 管理器中以模块方式安装官方 Release 包 → 重启实例。
4. **安装 LSPosed / Vector v2.2**：选择 Zygisk 版本安装 → 重启 → 确认管理器显示「已激活」。
5. **安装 Hide My Applist v3.8.3**：为运动世界启用隐藏模板（隐藏 Root 与模拟器相关应用）。
6. **安装 LocationSpoofer 2.6.13**：按原作者说明启用位置模拟；**不要升级到 3.x**。
7. **安装 SportSniffer 模块**：在 LSPosed 中对 `com.zjwh.android_wh_physicalfitness` 启用；启用后 App 内会监听 `127.0.0.1:8765` 提供签名服务。
8. **Windows 安装 Python 3.11.9**（勾选 Add to PATH）→ 安装依赖：

   ```bat
   py -m pip install pycryptodome==4.1.0
   ```

9. **部署本工具**：将本目录复制到电脑任意位置 → 双击 `一键跑步.bat`（首次会自动检查依赖）。
10. **校准配置**：若 MuMu 安装路径或 adb 端口不是默认值，按 `doctor` 自检提示修改 `config.json` 中的 `mumu_nx_dir` 与 `device`，再次运行 `python sport_run.py doctor` 直到全部 `[OK]`。
11. **试跑**：点「① 一键完成当天跑步」，按提示查看结果。

## 日常使用

1. 双击 **`一键跑步.bat`** 打开图形界面；
2. 工具自动：后台拉起 MuMu（如未开）→ 启动运动世界 → 连接签名服务 → 读取当前账号；
3. 点 **「① 一键完成当天跑步」**：约 3 秒发完请求，提示「已提交、核验中」；
4. 成绩核验完成后自动弹窗「成功」并显示有效里程；今天已跑过则约 2 秒提示「今天已完成」；
5. MuMu 自动隐藏到后台静默运行，不影响正常用电脑。

> 提交需在跑步时段 **06:00–22:00（服务器时间）** 内进行；单日有效里程上限 **2km**。

**命令行用法（可选）**：

```
python sport_run.py gui        图形界面（默认）
python sport_run.py status     查看环境 / 账号 / 学期里程
python sport_run.py run        命令行直接跑一次
python sport_run.py dry-run    只生成轨迹和报文，不提交
python sport_run.py doctor     环境自检
```

## config.json 字段说明

首次运行后自动生成：

| 字段 | 含义 |
|---|---|
| `mumu_nx_dir` | MuMu 的 `nx_main` 目录（含 `adb.exe`、`MuMuManager.exe`） |
| `device` | 实例 adb 地址，如 `127.0.0.1:16384` |
| `vmindex` | MuMu 实例序号（默认 `0`） |
| `hide_window_after_launch` | 启动后是否自动隐藏 MuMu 窗口（`true` 后台静默） |
| `boot_timeout_sec` | 等待安卓启动超时（秒） |
| `semester_id` | 学期 id

## 数据模板

轨迹与成绩报文由工具按接口返回的围栏与打卡点实时生成，本版**不再随仓库附带任何校区模板**。
若某个校区的打卡点接口临时不可用，需按本校区实际情况自行准备模板并放入 `data/` 目录
（`sample_trajectory_plain.json`、`save_2264_body.json`）。

本地运行产物（`config.json`、`last_submit.json`、`dryrun.json`、`dryrun_cipher.b64`）不纳入版本管理，已加入 `.gitignore`。


## 故障排查

| 现象 | 处理 |
|---|---|
| `doctor` 提示 App / 签名服务未就绪 | 确认运动世界已登录、SportSniffer 已在 LSPosed 中对目标应用启用；必要时重启一次运动世界 |
| 提示「未登录」 | token 过期，在 MuMu 内手动登录一次运动世界 |
| MuMu 未运行 | 工具会自动后台启动；若超时，手动打开 MuMu 进入系统后重试 |
| 提示「已提交、核验中」后要等几分钟 | 正常：服务端核验需要时间（约 5–12 分钟），工具会自动轮询并弹窗最终结果 |
| 记录保存成功但未计有效里程 | 当日 2km 额度已满，次日 06:00 重置后再跑 |
| 缺 pycryptodome | `py -m pip install pycryptodome==4.1.0` |

## 第三方组件与许可（重要）

- 本仓库**不包含**上述任何第三方组件（MuMu、运动世界校园、Zygisk Next、LSPosed/Vector、HMA、LocationSpoofer、SportSniffer）的源码或二进制，仅提供部署说明与引用；
- 各组件的名称、版权与许可归各自作者所有；使用前请阅读其官方文档与许可条款，并遵守所在地区法律法规；
- 特别提示：**Zygisk Next 自 v4-0.9.2 起不再是 GPL-3.0 开源项目**，改为保留所有权利，禁止修改、再分发或提取其代码与二进制；请仅从官方 Release 下载使用；
- **Vector 是 JingMatrix 维护的 LSPosed 续作**（GPL-3.0），官方 LSPosed 项目最后版本为 v1.9.2，仅支持到 Android 14；Android 15+ 环境请使用 Vector；
- 版本号如与官方最新发布不一致，以官方 Releases 为准，升级前请先验证兼容性。

## 免责声明

本项目仅供学习与个人自动化研究使用。请遵守学校体育考核规定与运动世界用户协议；因使用本工具产生的一切后果（包括但不限于成绩无效、账号受限）由使用者自行承担，作者不对未经授权的使用负责。

## 作者

baishuyue （MIT License）
